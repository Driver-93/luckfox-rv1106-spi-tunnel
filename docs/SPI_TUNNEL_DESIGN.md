# SPI 隧道设计 (Luckfox RV1106 master ↔ ESP32-C3 slave)

## 目标
板子通过 SPI 把 IP 数据包交给 C3，C3 用自己的 WiFi 联网，给板子提供网络接入。

## 为什么不能用现成方案
| 方案 | 为什么不行 |
|---|---|
| 内核 TUN/TAP | Luckfox 内核**没有 CONFIG_TUN**，且 overlay 加不上（base DTB 无 `__symbols__`） |
| SLIP | 内核无 slip 驱动 |
| PPPD | 板子无 pppd；C3 的 lwIP `PPP_SERVER` 默认 0 |
| USB NCM | C3 无 USB OTG |
| **自定义用户态隧道** | ← **唯一可行** |

**结论：两端都写用户态隧道，自己封装 IP 包。**

## 协议 (自定义, 简单可靠)

SPI 是主从、全双工、master 主导。C3 无法主动发起传输，所以：

```
Master (Luckfox) 主动发起每一次传输
Slave  (C3)      只能被动应答, 但要发数据时放在应答里 / 或用 IRQ 线通知
```

### 关键设计: 用一根 IRQ 线让 C3 能"主动"要数据

需要 **额外一根 GPIO 线** 从 C3 → Luckfox 做中断/就绪信号。
没有它的话 master 只能盲轮询，延迟和 CPU 都受不了。

### 帧格式

```
每帧固定 4KB:
┌────────┬────────┬────────┬────────┬──────────────┐
│ MAGIC  │ TYPE   │ SEQ    │ LEN    │ PAYLOAD      │
│ 4B     │ 1B     │ 4B     │ 2B     │ 最多 4081B   │
└────────┴────────┴────────┴────────┴──────────────┘
```

TYPE:
- `0x01` HELLO      握手
- `0x02` IP_PACKET  IP 数据包 (双向)
- `0x03` POLL       master 问: 有数据吗?
- `0x04` NODATA     没有
- `0x05` WIFI_STAT  C3 上报 WiFi 状态

### 流量方向

```
板子发数据 (上行):
  Luckfox IP 栈 → 隧道 → SPI 帧(IP_PACKET) → C3 → 注入 C3 的 lwIP → WiFi

板子收数据 (下行):
  WiFi → C3 lwIP → 拦截目标为板子的包 → 塞进 SPI 应答 → Luckfox 隧道 → IP 栈
```

### 地址方案 (关键难点)

板子需要自己的 IP。方案：
- C3 的 WiFi 接口 `192.168.3.x`
- 板子隧道口用 **第二个 IP**，比如 C3 做 ARP 代理 + NAT

**更简单可靠的方案：C3 做 NAT 路由器**
```
板子隧道网段: 10.7.7.2/24, 网关 10.7.7.1 (C3 的隧道口)
C3 在隧道口开 NA(P)T, 把 10.7.7.2 的流量 NAT 成 C3 自己的 WiFi IP
```

---

## 待确认的硬件接线

需要从 C3 到 Luckfox **再拉一根线**做中断：

| C3 引脚 | Luckfox 物理脚 | 作用 |
|---|---|---|
| 已接 GPIO? | ? | IRQ: C3 有数据要发给板子 |

Luckfox 剩余可用物理脚: **11(gpio41), 30(gpio71), 31(gpio144), 33(gpio135)**

⚠️ 但 pin 11 之前测出来是坏的(BR 反向不通)。所以用 **30 或 31**。

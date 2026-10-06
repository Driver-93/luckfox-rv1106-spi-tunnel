# PPP 方案分析

## 需求
- **Luckfox** 需要上网（图传 + 遥控）
- **ESP32-C3** 有 WiFi（能连外网）
- 两者用 UART 连

## 问题
流量必须从 Luckfox → ESP32 → WiFi(外网)

这意味着：
- Luckfox 需要一条**通往外界的网络接口**
- ESP32 需要**转发** Luckfox 的流量到 WiFi

## lwIP 能做什么

| 能力 | ESP-IDF lwIP | 说明 |
|------|-------------|------|
| PPP 客户端 (`pppapi_connect`) | ✅ | 连 4G 模组用这个 |
| **PPP 服务端** (`pppapi_listen`) | ❌ | **`PPP_SERVER=0`，未编译** |
| IP 转发 (`LWIP_IP_FORWARD`) | ✅ | 已开 |
| NAPT (`LWIP_IPV4_NAPT`) | ✅ | 已开 |

## 方向分析

**方案 A：Luckfox 做客户端，ESP32 做服务端**
```
Luckfox(pppd client) ──拨号──> ESP32(PPP server) ──NAT──> WiFi
```
- ❌ **ESP32 的 lwIP 不支持 PPP server**（`PPP_SERVER=0`）

**方案 B：ESP32 做客户端，Luckfox 做服务端**
```
ESP32(ppp client) ──拨号──> Luckfox(pppd server)
```
- 这样 Luckfox 是"服务端"，它给 ESP32 分 IP
- 但 **ESP32 才是需要外网的一方**，方向反了
- Luckfox 自己没外网，没法给 ESP32 转发
- ❌ 没用

**方案 C：Luckfox 做服务端 + ESP32 反向 NAT**
```
ESP32(ppp client) ──拨号──> Luckfox(pppd server, 分配10.0.0.2)
ESP32 把 ppp 接口的流量 NAT 到 WiFi
```
- 需要 ESP32 对 **ppp→wifi** 方向做 NAT
- lwIP 的 NAPT (`ip4_napt`) 支持任意接口对，**理论上可以**
- ⚠️ 但 ESP32 作为 PPP 客户端时，ppp 接口是它的"上行"，
  要让别的设备经它转发，得设 `ip4_napt_enable(ppp_netif, ...)`
- **可行性待验证** —— 这是唯一用 PPP 的方向

**方案 D：不用 PPP，用串口裸 IP 隧道**
```
Luckfox 上跑 daemon: tun/tap 或直接 socket -> 串口帧 -> ESP32 -> UDP -> WiFi
```
- 需要 Luckfox 有 **TUN**（之前查过：**没有**）
- 可以用 `socat` 做 TCP 代理，但延迟差
- ⚠️ 图传走这个会很卡

**方案 E：换 ESP32-S3（支持 USB OTG）**
```
Luckfox ──USB──> ESP32-S3(USB NCM 网卡) ──WiFi──> 外网
```
- S3 支持 USB OTG，且 tinyusb 组件支持 S3
- **最干净、最快（~10Mbps）**
- 但要买新板子

## 结论

**用 C3 + UART 做完整网络转发，只有方案 C 勉强可行，且不确定。**

实际上更靠谱的选择：
1. **方案 E**：买 ESP32-S3（约 20 元），USB 网卡方案成熟
2. **方案 F**：C3 做**串口 TCP 代理**（不是透明网桥）
   - Luckfox 把图传/控制都指向 ESP32 的串口代理
   - ESP32 转发到云端
   - 但这样图传要重新设计，且 UART 921600 只有 ~90KB/s，720p 图传不够

## 我的建议

**老实说，C3 做不了理想的透明网桥。** 两个选择：

- **① 买 ESP32-S3** —— USB 网卡方案，10Mbps，成熟，我固件都写好了（改个 target 就能编）
- **② 回到 UART PPP 方案 C** —— 我试一下 ESP32 反向 NAT 能不能work，成功率约 50%

**你倾向哪个？**

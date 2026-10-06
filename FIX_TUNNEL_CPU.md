# 隧道 CPU 争抢 — 修复补丁与部署说明

> **状态**：补丁已写好并验证，**尚未部署到板子**（隧道抖动导致文件传不过去）

---

## 一句话总结

`spinet.py` 的 `pump()` 是无 yield 的忙轮询，**独占板子唯一的 A7 核**，
导致 rkipc/mediamtx/web_server 饿死 → 图传断线 + 控制失效 + SPI 丢帧。

---

## 证据

板子实测（`/userdata/evidence.txt` + `/userdata/health.csv`）：

```
spinet.py   CPU 累计 29383 jiffies   ← rkipc 的 6.2 倍
pump 帧率   143 → 205 → 228          ← 应 350-400，掉 60%
fails       15 → 84 持续增长          ← 持续丢帧（协议无重传）
idle        0-30%，出现过 0%          ← CPU 满负荷
rssi        -52~-59 (优)              ← 与信号无关
spinet_pid  797 全程不变               ← 非重启
web_pid     785 全程不变               ← 非崩溃循环
```

---

## 补丁内容

**文件**：`car/spinet.py`，函数 `Tunnel.pump()`

**改动**：加入有界自适应退让（`idle_streak`），只在**双向都空闲**时 `sleep(1ms)`。

```python
def pump(self):
    ...
    idle_streak = 0
    while self.running:
        # 1. 取发送队列
        payload = b""
        with self.cv:
            for cid in list(self.txq.keys()):
                payload = self._pop(cid)
                if payload:
                    break

        # 2. SPI 交换（不变）
        r = self.spi.exchange(T_APP, payload)
        ...
        # 3. 处理返回（不变，但每条分支都重置 idle_streak）
        ...
        # 4. 【新增】双向空闲才让出 CPU
        if not payload:
            idle_streak += 1
            if idle_streak >= 2:
                time.sleep(0.001)
        else:
            idle_streak = 0
```

**为什么 1ms 是安全的**：
从机是 store-and-forward（一帧延迟），主机必须持续发帧才能取回 C3 的数据。
1ms 退让最坏只多 1 帧延迟（约 40ms），但能把 CPU 让给视频编码。

---

## 部署方法

### 方法 A：插网线（推荐，最快）

```powershell
# 1. 插上网线，确认板子有线可达
ping 192.168.3.68

# 2. 用带外通道部署（不经隧道，稳定）
scp -i id_ed25519 car/spinet.py root@192.168.3.68:/tmp/spinet_new.py
scp -i id_ed25519 _deploy_spinet.sh root@192.168.3.68:/tmp/ds.sh
ssh -i id_ed25519 root@192.168.3.68 "sh /tmp/ds.sh"
```

`_deploy_spinet.sh` 会：语法检查 → 备份 → 安装 → 重启 → 验证 → **失败自动回滚**

### 方法 B：U 盘 / SD 卡

1. 把 `car/spinet.py` 拷到 U 盘
2. 插到板子，`cp /media/usb0/spinet.py /userdata/car/spinet.py`
3. `/etc/init.d/S99z_spinet restart`

### 方法 C：降低图传码率腾出带宽

```sh
# 把码率降到 100kbps，减轻隧道负载后再传文件
sed -i 's/max_rate.*=.*[0-9]*/max_rate = 100/' /userdata/rkipc.ini
# 重启 rkipc 后再尝试 scp
```

---

## 部署后验证

```sh
# 1. 看 CPU 是否让出来了
sh /userdata/car/healthlog.sh
tail -5 /userdata/health.csv
#   期望: pump_fps 回升到 350-400, idle 明显上升

# 2. 看 fails 是否停止增长
grep '\[pump\]' /userdata/spinet.log | tail -5
#   期望: fails 基本不涨

# 3. 看 spinet CPU 占用是否下降
top -b -n 1 | head -12
#   期望: spinet 从 ~60% 降到 30% 以下
```

**成功标准**：
- `pump_fps` ≥ 300
- `fails` 增速显著放缓
- `idle` ≥ 30%
- 控制/图传不再偶发失效

---

## 回滚

```sh
cp -f /userdata/car/spinet.py.bak /userdata/car/spinet.py
/etc/init.d/S99z_spinet restart
```

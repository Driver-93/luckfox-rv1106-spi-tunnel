# 无网线远程访问 (SSH / 控制页 / 图传)

拔掉网线后，通过 `192.168.3.69`（ESP32-C3 的 WiFi 地址）访问板子的全部服务。

---

## 访问方式

| 用途 | 地址 | 状态 |
|---|---|---|
| **SSH** | `ssh root@192.168.3.69` | ✅ |
| **控制页** | `http://192.168.3.69/` | ✅ |
| **图传 (HLS)** | `http://192.168.3.69:8888/car/index.m3u8` | ✅ |
| **图传 (WHEP)** | `http://192.168.3.69:8889/car/whep` | ⚠️ 见下 |

> 版子自己的有线地址 `192.168.3.68` 拔网线后**不存在**，别用它。

---

## 端口转发表 (C3 侧)

C3 监听这些端口，每收到连接就通过 SPI 隧道请板子连自己的
`127.0.0.1:<同号端口>`：

| C3 端口 | 板子服务 |
|---|---|
| 80 | web_server (控制页) |
| **22** | **sshd (远程 SSH)** |
| 8889 | mediamtx WebRTC/WHEP 信令 |
| 8189 | mediamtx ICE (TCP) |
| 8888 | mediamtx HLS |

配置在 `spi_tunnel_c3/main/main.c` 的 `REV_PORTS[]`。

---

## 图传为什么走 HLS 而不是 WebRTC

**WebRTC 的媒体流走 UDP/ICE，而 SPI 隧道只能承载 TCP，所以拔网线时 WHEP 必然失败。**

方案：
1. 板子 mediamtx 开启 **HLS** (`hls: yes`, `hlsAddress: :8888`)，纯 HTTP/TCP，能穿隧道
2. 网页先试 WHEP，失败后**自动回退到 HLS**（`index.html` 里的 `playHLS()`）

**代价**：HLS 延迟比 WebRTC 高（约 3-6 秒），但能用。

---

## 带宽账本

| 项目 | 数值 |
|---|---|
| SPI 链路理论上限 | **1 MB/s** (24MHz, 272 帧/s × 4000B) |
| 端到端实测 (优化后) | **~65 KB/s (523 kbps)** |
| 瓶颈 | 板子单核 CPU |
| 720p 原始码率 | 1024 kbps ❌ 塞不下 |
| **调低后码率** | **320 kbps** ✅ 有余量 |

**视频码率在 `/oem/usr/share/rkipc-300w.ini`**：`max_rate` 1024 → 320。

### CPU 占用 (优化后)

| 进程 | CPU | 说明 |
|---|---|---|
| `spinet.py` | 36% | SPI 隧道（必要） |
| `rkipc` | 14% | 视频编码（必要） |
| `web_server.py` | **0%** | 优化前 25% |
| `mediamtx` | 0% | — |
| **idle** | **28%** | 优化前 **0%** |

---

## 三个隐蔽的坑（按影响排序）

### 1. C3 反向连接槽泄漏 → 堆耗尽 → 隧道瘫痪 ★最严重

**症状**：图传突然完全断掉，控制页也打不开。C3 串口显示：

```
err=56280                  ← 每帧都错 (正常 ~20)
free=42208 minfree=7072    ← 堆从 159KB 掉到 42KB
槽位 5/10 占用              ← 5 个连接卡住不放
```

**根因**：`rev_worker` 有多个 `break` 出口，部分路径不释放槽位。
每个卡住的槽占 8KB 缓冲 + 4KB 栈，5 个就吃掉 60KB+，
堆见底后 SPI 每帧分配失败 → 隧道彻底失效。

**修法**：
- worker 出口统一 `revproxy_finish(r->id)`
- 新增**空闲回收任务** `rev_reaper_task`：90 秒无活动的连接强制关闭
- 缓冲 32KB→4KB，槽位 16→8（最坏 32KB，留足余量）

### 2. 无网线时 ffmpeg 推云端白烧 CPU

`relay_loop.sh` 不停重启 ffmpeg 推流到 `YOUR_SERVER_IP:8554`，
**没网线根本连不上，却占 23% CPU**。

**修法**：禁用 `S97relay`，并让独立看门狗每 20 秒清理残留的
`relay_loop` / `ffmpeg`。

### 3. 软件 PWM 空转 1000 Hz ★意外的大头

`car_motor.py` 的 `_pwm_loop` 以 1 kHz 运行，**电机停着时每轮仍然
对 4 个 GPIO 各写一次 = 4000 次无用写入/秒**，实测吃掉 `web_server.py`
25% CPU —— 而板子是**单核**，这些 CPU 本该给隧道和视频。

**修法**：
- 记住上次写入值，值没变就不碰 GPIO
- 四路都为 0 时直接睡 50ms

**效果**：`web_server.py` 25% → **0%**，idle 18% → **28%**，
视频分片吞吐 285 → **523 kbps**（+83%）。


---

## 防止"把自己关在门外"

**问题**：没网线时 SSH 和控制页都走隧道。停掉隧道服务 = 彻底失联，
因为 SSH 本身也需要隧道才能进来。（踩过一次，只能断电重启。）

**解决**：独立看门狗 `/etc/init.d/S99yy_spinet_wd`

- 每 20 秒检查隧道，挂了自动拉起
- **不归 `S99z_spinet` 管**，所以 `S99z_spinet stop` 杀不掉它
- 实测：停掉隧道后 **10 秒内自动恢复**
- 日志：`/userdata/spinet_wd.log`

> `S99z_spinet` 自己也有个内置看门狗（60 秒），但 `stop` 会把它一起杀掉，
> 所以独立那份才是真正的保险。

---

## 常用命令

```sh
# 板子状态 (经隧道)
ssh root@192.168.3.69 "/etc/init.d/S99z_spinet status; /etc/init.d/S99yy_spinet_wd status"

# 看隧道日志
ssh root@192.168.3.69 "tail -20 /userdata/spinet.log"

# 看门狗日志
ssh root@192.168.3.69 "tail -20 /userdata/spinet_wd.log"

# 临时停隧道 (看门狗会在 20 秒内自动拉起)
ssh root@192.168.3.69 "/etc/init.d/S99z_spinet stop"
```

---

## 关键文件

| 文件 | 作用 |
|---|---|
| `spi_tunnel_c3/main/revproxy.c` | C3 多端口反向代理 |
| `spi_tunnel_c3/main/main.c` | `REV_PORTS[]` 端口表 |
| `car/spinet.py` | 板子侧隧道 + 代理 |
| `car/S99z_spinet` | 隧道自启 |
| **`car/S99yy_spinet_wd`** | **独立看门狗 (防失联)** |
| `car/index.html` | 控制页 (含 HLS 回退) |
| `car/mediamtx_rev.yml` | mediamtx 配置 (启用 HLS) |

---

## 独立看门狗安装 (新板子)

```sh
scp car/S99yy_spinet_wd root@<ip>:/tmp/S99yy_spinet_wd
ssh root@<ip> "cp /tmp/S99yy_spinet_wd /etc/init.d/ && chmod 755 /etc/init.d/S99yy_spinet_wd && /etc/init.d/S99yy_spinet_wd start"
```

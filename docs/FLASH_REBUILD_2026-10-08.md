# 2026-10-08 刷机重建 + 隧道/图传/遥控恢复实录

> 这篇是一次完整"刷机后重建"的现场记录。包含**实测数据**、**踩过的坑**、
> 以及**尚未解决的问题**。目的是让下次不必重新摸索。

---

## 一、起点：两个真正的根因

### 根因 1：SPI 时钟 24 MHz 太快 → 采样错位

**症状**（板子日志）：
```
[pump] 100 frames/s, 0.0 KB/s up (fails=80856, retx=0, ip tx=2 rx=517 drop=0)
                                  ^^^^^^ 每 2 秒 +200 —— 每一帧都失败
```

**证据链**：
- `/userdata/spi_speed` **不存在** → `spinet_c` 用默认 **24 MHz**
- C5 侧 `sp rx=0`，且**完全看不到板子的 HELLO**（帧内容全错，不是回程问题）
- `spitun.c` 注释早已写明："20MHz 在飞线+无屏蔽下跑不动…恒定错误 magic…典型的采样错位"
- 固件 `spi_slave.c:150-156` 为提高 MISO 驱动能力专门设了 `DRIVE_CAP_3`，
  并承认"20MHz 上限可能与信号完整性有关"

**修复与实测**：

| 时钟 | 帧率（空闲） | 帧率（负载） | fails | 隧道往返 |
|---|---|---|---|---|
| 5 MHz | 72-80 | — | 0 | 47.6 ms |
| 8 MHz | 96 | — | 0 | — |
| 12 MHz | 106 | — | 0 | — |
| 16 MHz | 115 | — | 0 | — |
| **20 MHz** | **119** | **173-182** | **0** | **21.5 ms** |

> ⚠️ 20 MHz 在**空载**和**负载**（30 包 ping）下都验证过 `fails=0`、0% 丢包。
> 但**图传满负载**（约 300 KB/s）下观察到 `fails=139` —— 见第五节遗留问题。

### 根因 2：两个 `spinet_c` 进程抢 SPI 总线

**证据**：`ps` 里两个实例；HELLO seq **乱跳**（907/923/908/914，两个独立计数器）。

**成因**：`S22spinet` 用 PID 文件杀进程，而看门狗 20 秒后会重拉 —— 两者撞车就留下重复实例。

**修复**：`mkdir` 原子锁 + `start()` 清残余 + 看门狗按计数处理，
并把看门狗健康判据**从"进程在不在"改为"数据面"**
（旧判据在 `fails=80856` 时仍报"健康"，因为进程确实活着 —— 这就是它一直没救回来的原因）。

---

## 二、刷机与重建

`upgrade_tool uf update.img` → `Upgrade firmware ok.`

### 刷机后必须手动补的东西（README 已提醒，但容易漏）

| 项 | 说明 |
|---|---|
| **`/root` 属主** | 原厂是 `1005`，sshd StrictModes 会拒收 authorized_keys → 必须 `chown root:root /root` |
| **`authorized_keys`** | 不存在，要自己装 |
| **板子 IP 会变** | 本次 `.87` → **`.88`** |
| **`/userdata` 全清** | `car/`、`hwcfg/`、`spi_speed` 全丢 |
| **`/usr/lib/` 全清** | **NPU 模型丢了**（这是 NPU 失效的根因） |

### 必须重建的文件

| 文件 | 来源 |
|---|---|
| 7 个 app 文件 | `board/app/` |
| `spitun.ko` + `tun.ko` | 前者用 `tools/build` 重编 |
| 4 个 overlay (spi0_spitun/spi0_spidev/pwm4/uart1_gps) | `board/dts/`，板上 dtc 编译 |
| **`car_config.json`** | ⚠️ **PC 侧备份是旧接线**！必须用 `board/config/` 的 2026-10-08 定稿 |
| mediamtx (38MB) | 放 `/root/mediamtx/`（**不能放 /userdata，只有 2.2MB**） |
| NPU 模型 | `tools/npu/models/` → `/usr/lib/` |

---

## 三、NPU 修复

**根因**：刷机清掉了 `/usr/lib/object_detection_pfp*.data`。

**修复**：推回模型 + 重启 rkipc。

**关键**：`rkipc` 的 `LD_LIBRARY_PATH` **必须含 `/usr/lib`**：
```
LIBPATH=/oem/usr/lib:/usr/lib:/oem/lib
              ^^^^^^^^ NPU 模型目录
```
少了它 → `rockiva` 初始化失败 → 网页没有检测框。

**验证证据**：
```
[rockiva.c][rkipc_rockiva_init]:ROCKIVA_Init over
[rockiva.c][rkipc_rockiva_init]:ROCKIVA_BA_Init success
/proc/<pid>/fd → /dev/rknpu 已打开
/sys/module/rknpu/refcnt = 2
```

---

## 四、遥控修复

**根因（两个）**：
1. **`car_config.json` 丢失** → 电机没有引脚定义
2. **PWM overlay 未加载** → `/sys/class/pwm/` 空的 → 电机无法调速

**注意引脚**：PC 备份里是**旧接线**（FL=65/64/67 等），**会接错**。
正确值（与 `docs/WIRING.md` 一致）：

| 轮 | PWM | AIN1 | AIN2 | pwmchip |
|---|---|---|---|---|
| FL | 57 | 56 | 72 | 10 |
| FR | 52 | 53 | 54 | 8 |
| BL | 73 | 59 | 58 | 6 |
| BR | 55 | 65 | 64 | 11 |

**验证**（`/api/motordbg`）：
```json
{"motor_ok": true, "mode": "hw", "pwm_alive": true, "pwm_err": 0,
 "dir_writes": 44, "pwm_writes": 44}
```

---

## 五、遗留问题（尚未解决）

### 1. 🔴 图传满负载下 20 MHz 出错
```
[pump] 117 frames/s, 254 KB/s up (fails=139, retx=8, ...)
```
空载稳、**满负载不稳**。需要降到一个负载下也稳的时钟
（12-16 MHz 是候选，5 MHz 已验证稳但延迟翻倍）。

### 2. 🔴 NPU 自启竞态
`S21appinit` → `RkLunch.sh` **抢先**起了 rkipc（原厂环境，`LIBPATH` 缺 `/usr/lib`），
我的 `S25rkipc` 检测到"已在运行"就让位退出 → **NPU 没加载**。

需要让 rkipc 用带 `/usr/lib` 的环境启动（或改 `RkLunch.sh` 的环境）。

### 3. 内核模块 netdev 未打通
`spitun.ko` 已能加载且 **probe 成功**（`dmesg: SPI ready: bus=0 cs=0`），
但数据面不通：
```
tx_packets=0  tx_dropped=36
dmesg: spitun: xmit#N len=0      ← 内核交给驱动的是空 skb
/proc/net/snmp: InAddrErrors 增长 ← 注入的包被拒
```
已修 `dev->type = ARPHRD_NONE`（原来漏设，是 0=ARPHRD_VOID），
但 `xmit len=0` 仍在。**用户态 `spinet.py` 用真 TUN 没这问题** ——
说明真 TUN 路线可行，值得一试。

---

## 六、实测性能

| 指标 | 值 |
|---|---|
| **控制延迟 p50** | **28.5-30.1 ms** ✅（<50ms） |
| p90 | 35.3 ms |
| WiFi 层（PC→C5） | 2-3 ms |
| 隧道往返（板→C5） | 21.5 ms @20MHz |
| 隧道帧率 | 117-182 帧/s |
| 图传 | H.264，mediamtx `2 tracks (H264, G711)` |

> ⚠️ **测量工具本身有坑**：PowerShell `Invoke-WebRequest` 报 65-86ms，
> 但那是它的固定开销。用**原始 TcpClient + keep-alive** 才是真实值（28.5ms）。
> 板内 localhost 基准 3.57ms，可作对照。

---

## 七、踩过的坑（避免重犯）

| 坑 | 现象 | 正解 |
|---|---|---|
| overlay 与实现不匹配 | 用户态泵 + spitun overlay → 泵起不来（缺 `/dev/spidev0.0`） | overlay 必须与实现配套 |
| `setsid` 缺失 | rkipc 日志正常、554 在听，随后 mediamtx 报 `connection refused` | 必须 `setsid`，否则脚本退出时进程被带走 |
| `rmmod` 卡死 | `Segmentation fault`，`lsmod` 显示 `-1` 引用计数 | 只能靠重启清除 |
| PowerShell 写脚本 | 加 UTF-8 BOM → `#!/bin/sh` 失效 → `Syntax error: "}" unexpected` | 用 `edit` 工具改，别用 `Set-Content` |
| `ps \| grep mediamtx` | 脚本**把自己 kill 掉**（自己的命令行含 "mediamtx"） | 用 `pidof` 或读 `/proc/*/comm` |
| `/userdata` 写满 | `cp` **静默失败**，看似部署成功实际没变 | 先 `df`，且给日志加轮转 |
| 刷机后旧备份 | PC 备份的 `car_config.json` 是**旧接线** | 以 `board/config/` 的定稿为准 |

---

## 八、自启脚本清单（本次补齐）

| 脚本 | 作用 |
|---|---|
| `S21spitun` | SPI overlay（spidev 或 spitun，必须与实现配套） |
| `S21uart1` | GPS UART1 |
| `S22pwm` | **4 路硬件 PWM overlay**（电机调速，本次新建） |
| `S22spinet` | 用户态泵（或 `S22b_spitun_kmod` 内核模块，二选一） |
| `S23web` | 网页服务 |
| `S24spinet_wd` | 泵看门狗（单实例锁 + 数据面判据） |
| `S25rkipc` | 相机（带 NPU 环境） |
| `S26mediamtx` | 图传（WebRTC） |

> 依赖顺序：`S21spitun`(overlay) → `S22b/spinet`(隧道) → `S23web` → `S25rkipc` → `S26mediamtx`

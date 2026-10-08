# Luckfox Pico Pro Max 遥控小车

[English](README.md) · **中文**

一台**单核 Linux 小车**的完整实现：低延迟图传、网页遥控、失控保护、
NPU 人/宠物检测、GPS，以及本项目的核心 —— **用 ESP32-C5 当 SPI 从机、
把一块没有无线网卡的开发板整个接入 WiFi 的网络隧道**。

```
      ┌──────────────┐   WiFi    ┌────────────┐  SPI 20MHz    ┌─────────────────┐
      │  浏览器/手机  │ ────────► │  ESP32-C5  │ ◄───────────► │  Luckfox RV1106 │
      │  控制 + 图传  │  WebRTC   │  WiFi 桥   │  4096B 帧     │  单核 Cortex-A7 │
      └──────────────┘  ICE-TCP  │  + NAPT    │  spinet_c 泵  │  tun0 = 10.77.0.2
                                 └────────────┘               └─────────────────┘
```

板子在 WiFi 网段上**没有 IP** —— 它和外界唯一的通路就是这条 SPI 隧道。
图传（WebRTC）和控制指令全部从这一根 SPI 线里过。

---

## 当前状态（2026-10-08）

| 项 | 状态 |
|---|---|
| 图传 | H.264 720p 25fps，WebRTC (ICE-TCP) 过隧道，端到端 |
| 隧道 | **C 泵**（用户态），推流负载 6-8% CPU，340 帧/秒，帧级重传零丢包 |
| 控制 | p50 ~28ms，失控保护（0.5s 无心跳自动停车） |
| NPU | 人/脸/宠物检测（rkipc 内置推理），宠物也画框，+3ms 延迟 |
| GPS | UART1 (GPIO 68/69)，NMEA 解析，页面上报定位/卫星数 |
| 遥控页面 | 自适应（PC/手机），摇杆 + 原地转 + 速度，状态面板 |

## 目录结构

```
firmware/c5-tunnel/     ESP32-C5 侧固件 (SPI 从机 + WiFi + NAPT + 端口映射)
board/
  app/                  板子侧应用
    spinet.py           隧道泵 (Python 版, 现作为 C 版的回退)
    web_server.py       网页服务 :80 (API / 失控保护 / 遥测缓存)
    car_motor.py        TB6612 四路电机 (硬件 PWM + 失控保护)
    sys_stats.py        系统状态采集 (轻/重字段分离)
    index.html          遥控页面 (单文件)
    video_ctl.py        图传档位/曝光 (rkipc 配置管理)
    cam_ctl.py          摄像头参数
  spinet_c/spinet.c     隧道泵 **C 版** (当前主力, 见下)
  init.d/               开机启动链 (文件名与板子完全一致)
  config/               配置模板 (car_config / mediamtx)
  dts/                  设备树 overlay (SPI / PWM / UART1)
driver/spitun.c         内核态隧道 (历史方案, 已被 spinet_c 取代, 见 docs)
tools/
  build/                交叉编译
  deploy/               部署 (刷机后一键重建 / 模块热替换)
  diagnose/             测量与观测
  npu/                  NPU 检测 (模型 / 改过的 rkipc / 探针)
docs/                   文档 (设计 / 踩坑 / 运维)
```

### 启动链（`board/init.d/`，文件名与板上一致，拷过去就能用）

| 脚本 | 用途 |
|---|---|
| `S21spitun` | 挂 configfs + 应用 SPI0 overlay (spidev) + 加载 tun.ko |
| `S21uart1` | 应用 UART1 overlay（GPS，GPIO 68/69） |
| `S22pwm` | 应用 4 路硬件 PWM overlay（电机调速） |
| `S22spinet` | 启动 **spinet_c**（无则回退 spinet.py）+ tun0 地址 + 回程策略路由 |
| `S23web` | web_server.py（:80，控制 + 图传页面） |
| `S24spinet_wd` | 隧道看门狗（进程消失自动拉起） |
| `S25rkipc` | 摄像头（检测 SC3336 应答后才启动，避免无效启动） |
| `S26mediamtx` | 图传服务（拉 rkipc RTSP → WebRTC；HLS 按需，不再 24h 转封装） |

> 命名注意：`S22spinet`/`S24spinet_wd` 的 "spinet" 是历史名字（隧道曾是
> Python 进程）。当前隧道泵是 `board/spinet_c/spinet.c` 编译出的 C 程序。

### 隧道泵为什么是 C（spinet_c）

隧道是 **340 次/秒的全双工 SPI 交换循环**，每帧的固定开销决定一切：

| 实现 | 推流负载 CPU | 说明 |
|---|---|---|
| Python (spinet.py) | 17~25% | 解释器税 ~0.5ms/帧 |
| 内核模块 (spitun.ko) | ~1-3% | 曾上线，但对 C5 固件未充分验证，**已搁置** |
| **C 用户态 (spinet_c)** | **6~8%** | 当前方案：拿到几乎全部内核版收益，崩了只是进程退出、看门狗拉起 |

spinet.py → spinet.c 是逐行移植，协议细节全部保留：滑动窗口 + 帧级重传、
每帧打包 3 个 IP 报文、空闲探测不消耗序号、C5 重启再同步、空闲退避
（1→4→8ms）。回退方式：删掉 spinet_c 二进制重启即回到 Python 版。

> ⚠️ 教训（两次事故换来）：**设备树 overlay / 内核模块只能在重启边界切换**，
> 运行中 insmod 或换 overlay 会因 IOMUX/属性泄漏导致引脚假死甚至内核崩溃。
> 详见 `docs/RECOVERY_REPORT.md`。

---

## 关键实测数据

| 指标 | 数值 |
|---|---|
| SPI 单帧 | 线上 1.64ms @20MHz，往返 ~3ms |
| 隧道吞吐 | 最高 9.2 Mbps（帧打包 + 滑动窗口） |
| 控制延迟（满载） | p50 36ms / p90 61ms，0 丢包（帧级重传） |
| 空载控制 | p50 ~25ms |
| 隧道泵 CPU | 推流 6-8%（Python 版 17-25%） |
| NPU 检测 | +3ms 控制延迟（npu_fps=15），人/脸/宠物 |
| web_server 空闲 | 6%（sys_stats 轻/重字段分离后，原 15%） |

---

## 硬件

| 部件 | 型号 / 说明 |
|---|---|
| 主控 | Luckfox Pico Pro Max（RV1106，单核 Cortex-A7，128MB） |
| WiFi 桥 | ESP32-C5（SPI 从机 + NAPT + 端口映射 80/22/554/8889/8189） |
| 摄像头 | SC3336 3MP（CSI，rkipc 编码 H.264） |
| 底盘 | 4WD 麦克纳姆轮 + TB6612 ×2 |
| GPS | NMEA 串口模块（UART1，GPIO 68/69） |
| NPU | RV1106 NPU，rockiva PFP 模型（人/脸/宠物） |

### 电机接线（2026-10-08 定稿，每路顺序 PWM / AIN1 / AIN2）

| 轮 | PWM | AIN1 | AIN2 | pwmchip |
|---|---|---|---|---|
| 左前 FL | 57 | 56 | 72 | 10 (pwm10m2) |
| 右前 FR | 52 | 53 | 54 | 8 (pwm8m1) |
| 左后 BL | 73 | 59 | 58 | 6 (pwm6m1) |
| 右后 BR | 55 | 65 | 64 | 11 (pwm11m1) |

> ⚠️ **不可用引脚黑名单**（实测踩坑）：
> GPIO 42/43 —— 调试串口脚，输出驱动失效（写 0 读 1）；
> GPIO 71 —— 250607 固件无 PWM 功能；
> 电池 ADC = SARADC_IN1（GPIO 145 / 物理 32），分压比见 car_config。
> 完整引脚表与变更史见 [`docs/WIRING.md`](docs/WIRING.md)。

> ⚠️ **电机必须独立供电**。USB 带电机 → 欠压 → 隧道断连
> （C5 日志证据：`E BOD: Brownout detector was triggered`）。

---

## 部署

**整机重建（刷机后）**：

```bash
# 刷机: 板子 USB 进 MaskRom, 用 SocToolKit 的 upgrade_tool 命令行即可
#   upgrade_tool uf Luckfox_Pico_Pro_Max_Flash_250607/update.img
# 然后一键重建 (通过 adb/ssh 均可, 脚本自动装回全部服务):
tools/deploy/_postflash_restore.sh <板子IP>
# 注意补两个 /userdata 文件: hwcfg/spi0_spidev.dts + hwcfg/tun.ko (见脚本输出)
```

**日常应用更新**：

```bash
scp board/app/* root@<板子IP>:/userdata/car/
scp board/init.d/S* root@<板子IP>:/etc/init.d/   # 文件名必须一致
scp board/spinet_c/spinet_c root@<板子IP>:/userdata/car/   # 可选
```

**配置**：`board/config/car_config.example.json` → 填电机引脚 / MQTT / GPS 串口。
真实密码不入库（模板里是 CHANGE_ME）。

**NPU 检测**（可选）：模型放 `/usr/lib/`，`enable_npu=1`、`npu_fps=15`（ini 和
模板两处都要改）；让狗也出框需换 `tools/npu/rkipc-pet-6/` 的 rkipc。
详见 [`docs/NPU_DETECTION.md`](docs/NPU_DETECTION.md)。

---

## 界面

PC / 手机同一套页面，自适应：

![控制界面 - PC](docs/images/ui-desktop.png)

| 手机端 | 遥测与调试面板 |
|---|---|
| ![手机](docs/images/ui-mobile.png) | ![调试面板](docs/images/ui-debug-panels.jpg) |

画面内是播放器风格的 OSD：左上角常驻 `RTT · 图传速率` HUD（按延迟上色），
曝光/画质为弹出档位菜单（ISP 参数必须重启 rkipc，做不了滑条），失控保护
误停率超 5% 时页面变红告警。原地转是两个圆角三角按钮（按住即转、松手回正）。
设计取舍与踩坑细节见 [`docs/PROGRESS.md`](docs/PROGRESS.md)。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/SPI_TUNNEL_DESIGN.md`](docs/SPI_TUNNEL_DESIGN.md) | 隧道协议与设计 |
| [`docs/SPI_LATENCY_ANALYSIS.md`](docs/SPI_LATENCY_ANALYSIS.md) | 延迟瓶颈实测分析 |
| [`docs/WIRING.md`](docs/WIRING.md) | 硬件接线终稿 + 引脚黑名单 |
| [`docs/RECOVERY_REPORT.md`](docs/RECOVERY_REPORT.md) | 刷机重建 / 事故复盘 / 运维手册 |
| [`docs/NPU_DETECTION.md`](docs/NPU_DETECTION.md) | NPU 检测部署与调优 |
| [`docs/VIDEO_RESTART.md`](docs/VIDEO_RESTART.md) | 重启 rkipc 的正确姿势 |
| [`docs/USERDATA_SPACE.md`](docs/USERDATA_SPACE.md) | /userdata 只有 2.2MB 的部署纪律 |
| [`docs/TIME.md`](docs/TIME.md) | 时钟为什么故意不设时区 |
| [`docs/ISSUES.md`](docs/ISSUES.md) | 已知问题清单 |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | 完整开发过程与踩坑记录 |

---

## 许可

仅供学习参考。涉及真实硬件，请自行评估安全风险 —— **遥控车务必先做好失控保护**。

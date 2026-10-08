# Luckfox Pico MAX 4G 遥控小车 — 接线与调试状态说明

> 更新: 2026-09-08  实测调通情况见每节【状态】标注

## ⚠️ 通用前提
- USB 切 host 模式后 ADB 失效，用网线+SSH（已配置免密 `sshx.ps1` → `SshB "命令"`）访问
- 板子: `root@192.168.3.66`（Luckfox Pico Pro Max, RV1106, 内核 5.10.160）
- 服务器: `ubuntu@150.109.12.233`（密钥 `Documents\car.pem`，EMQX + mediaMTX 都在上面）
- 所有 GPIO 在 `/etc/luckfox-car/car_config.json` 配置

## 引脚对照（官方 40pin 实测确认）
| 物理脚 | GPIO名称 | Linux GPIO | 功能 |
|--------|----------|-----------|------|
| 1/2   | GPIO1_B2/B3 | 42/43 | UART2 (调试串口 ttyFIQ0, **不要接外设**) |
| 6/7   | GPIO1_C5/C4 | 53/52 | **UART4 TX/RX (m1)** ← GPS 用这个 |
| 12/14 | GPIO1_C0/C1 | 48/49 | UART3 (m1), 备用 |
| 19/20 | GPIO1_D0/D1 | 56/57 | UART3 TX/RX 备用 |
| 21/22 | GPIO0_A4/A5 | 4/5   | **无 UART 功能**(pmic/i2c1/pwm), 官方图标注有误 |
| 32    | GPIO4_C1 | - | **SARADC_IN1** ← 驱动板 ADC 用这个 |
| 31    | GPIO4_C0 | - | SARADC_IN0 |
| 36    | 3V3(OUT) | - | 3.3V 输出 |

## 1. SC3336 摄像头 → CSI【✅ 已通】
- `rkipc` 自启, RTSP: `rtsp://192.168.3.66:554/live/1`
- ffmpeg 中继循环把流推到服务器 `rtsp://150.109.12.233:8554/car` (mediaMTX)

## 2. TB6612 四路驱动【✅ 四轮全通 - 2026-09-22】
实际接线与配置 (已逐个实测确认, 四轮前进方向均正确):

| 通道 | 轮子 | 驱动板信号 | 板子物理脚 | GPIO | 配置项 |
|------|------|-----------|-----------|------|--------|
| C | 左前 | CIN1/CIN2/PWMC | (2026-10-08 重接) | 56/72/57 | FL (PWM=57=pwm10m2, chip10) |
| A | 右前 | AIN1/AIN2/PWMA | (2026-10-08 重接) | 53/54/52 | FR (PWM=52=pwm8m1, chip8) |
| D | 左后 | DIN1/DIN2/PWMD | (2026-10-08 重接) | 59/58/73 | BL (PWM=73=pwm6m1, chip6) |
| B | 右后 | BIN1/BIN2/PWMB | (2026-10-08 重接) | 65/64/55 | BR (PWM=55=pwm11m1, chip11) |

> 2026-10-08 最终定稿（用户确认）。已弃用/不可用引脚：**42/43**（调试串口脚，
> 写不进低电平）、**58 曾因 overlay 泄漏假死**（重启恢复，可用）、**71**（250607
> 固件无 PWM 功能）。GPS: uart1 GPIO 68/69（物理 21/22），PPS=64。
| — | — | STBY | **未接 (悬空)** | — | 靠驱动板内部上拉 |

### 🔴 重要: 物理脚 17/19/20 损坏/不通 (已弃用)
- 引脚图确认 `物理17=GPIO1_B0(40)`, `物理19=GPIO1_D0(56)`, `物理20=GPIO1_D1(57)`
- 但实测: 板端 gpio40/56/57 `dir=out` 写1读回1 正常, **接上驱动板却不转**
- 对调验证: C通道线插到 17/19/20 -> 左前轮不转; D通道线插到 14/15/16 -> 左后轮转
- **结论: 板子这三个物理脚有问题, 已弃用** (猜测虚焊或走线断)
- 现已把 C通道(左前轮)改接到 **24/25/27** (GPIO2_A0/A1/A3 = gpio 64/65/67), 实测正常

### 引脚图推导公式 (Luckfox Pico Max)
`Linux GPIO = 32 × bank + (port - 'A') × 8 + index`
例: `GPIO1_C1` = 32×1 + 2×8 + 1 = **49**;  `GPIO2_A3` = 32×2 + 0 + 3 = **67**
(引脚图: `docs/Luckfox-Pico-Max-pinout.jpg`)

### 已占用引脚 (勿再分配)
| 用途 | 物理脚 | GPIO |
|------|--------|------|
| 电机 A/B/C/D | 4,5,9,10,11,12,14,15,16,24,25,27 | 54,55,58,59,41,48,49,50,51,64,65,67 |
| GPS uart1 TX/RX | **22 / 21** | **69 / 68** ← 2026-10-08 改接 (uart1m1, overlay: /userdata/hwcfg/uart1_gps.dts, 开机 S21uart1 应用; 软件读 car_config.json 的 gps_uart=/dev/ttyS1) |
| GPS PPS | 26 | 66 |
| 电池 ADC (SARADC_IN1) | 32 | 145 |
| 电源/GND/NC | 3,8,13,18,23,28,34,35,36,37,38,39,40 | — |

### 空闲可用物理脚 (备用)
`1(42) 2(43) 21(68) 22(69) 29(70) 30(71) 31(144) 33(135)`
注: 34 脚是 **NC(未连接)**, 所以 STBY 无处可接, 只能靠驱动板内部上拉

### ✅ 运动模型验证 (MQTT 实测)
| 指令 | FL | FR | BL | BR | 正确性 |
|------|----|----|----|----|--------|
| forward | F | F | F | F | ✅ |
| backward | R | R | R | R | ✅ |
| spin_left | F | R | F | R | ✅ |
| spin_right | R | F | R | F | ✅ |
| strafe_left | R | F | F | R | ✅ |
| strafe_right | F | R | R | F | ✅ |
| stop | - | - | - | - | ✅ |

### ⚠️ 供电 (必须电池, 不能用板子USB)
- 之前电机走板子 USB 供电 -> 电机启动拉垮供电 -> **板子欠压掉线** (云端日志 i/o timeout)
- 改电池供电后: 板->云丢包从 **20~23% 降到 0%**
- TB6612 VM 接电池(5.5~15V), VCC 接板子 3V3, **GND 必须共地**


实际配置见 `car_config.json`。PRODUCTION.md:45 的物理脚↔GPIO 对照表为准:

| 通道 | 驱动板信号 | 板子物理脚 | GPIO | 配置项 | 状态 |
|------|-----------|-----------|------|--------|------|
| C 左前 | CIN1/CIN2/PWMC | 14/15/16 | 50/49/51 | FL | ✅ 已实测可用 |
| D 左后 | DIN1/DIN2/PWMD | 17/19/20 | 40/56/57 | BL | 未接线 |
| A 右前 | AIN1/AIN2/PWMA | 4/5/9 | 55/54/58 | FR | 未接线 |
| B 右后 | BIN1/BIN2/PWMB | 10/11/12 | 59/41/48 | BR | 未接线 |
| STBY | STBY | 34 | — | — | **未接线(悬空)** |

### ✅ 实测结论 (2026-09-11) - 车轮动力已打通
- **目前只接了 1 个电机** = C 通道 (物理 14/15/16 → gpio 50/49/51)
- 逐脚独立实测 `GPIO(g,'out')` + `write(True)` 后读回 sysfs value:
  - gpio49 (CIN1/14) **可拉高 ✅**
  - gpio50 (CIN2/15) **可拉高 ✅**
  - gpio51 (PWMC/16) **可拉高 ✅**
- **STBY 未接线**, TB6612 的 STBY 默认上拉为高(使能), 所以不接也能工作
  - gpio7 读回恒为 0 是正常的(那根线不存在), **不是故障**
  - 注意: 若驱动板 STBY 无内部上拉, 需接 3V3 或接 gpio7 并软件拉高

### ✅ 方向/调速实测 (真实电机模式, `S99car start`)
| 指令 | IN1(50) | IN2(49) | 结果 |
|------|---------|---------|------|
| forward | 1 | 0 | ✅ 正转 |
| backward | 0 | 1 | ✅ 反转 |
| stop | 0 | 0 | ✅ 停 |

- PWM 占空比随 speed 变化 (30/60/90 -> 实测 15.6%/72.6%/100%), stop 时为 0% ✅

### 🔧 修复: 软件 PWM 四通道串行 bug (car_motor.py `_pwm_loop`)
- **原实现**: 4 个通道串行放在一个 while 循环里, 每通道 `sleep(on)+sleep(off)`
  走完整个周期 -> 4 通道时实际频率降为 `_freq/4`, 相位错开、抖动大
- **新实现**: 以同一时间基准调度, 周期开始统一拉高, 各通道到点拉低, 再对齐下一周期
  -> 4 通道同步, 频率不随通道数下降
- 实测 `periphery` 单次 GPIO 翻转仅 21.7us, 1kHz (500us) 完全可实现

### ⚠️ 更正说明 (重要)
本文档此前记录的"5 个引脚无法输出高电平 / 需改 DTB pinmux"是**错误结论**。
成因: 当时的测试脚本一次性打开全部 13 个引脚并在紧循环里连续翻转,
导致读数失真。改为**逐脚独立测试**后, 全部引脚正常。
**板子 pinmux 无需任何改动, 也未做任何 DTB/overlay 修改。**

### 🔴 BL(D通道) 定案: 物理脚 17/19/20 不通 (2026-09-22)
**排除法结论 (用户实测 + 板端验证)**:
| 测试 | 结果 |
|------|------|
| D通道马达 接到 C通道输出 | ✅ 转 -> 马达+驱动板D通道都好 |
| C通道线插到物理17/19/20, 测BL(gpio40/56/57) | ❌ **左前轮不转** -> **物理脚17/19/20 不通** |
| FL配置(gpio50/49/51) 驱动物理14/15/16 | ✅ 转 |

- 板端验证: gpio 40/56/57 `dir=out`, 写1读回1, pinmux 为 `MUX UNCLAIMED`(GPIO功能),
  **软件与SoC侧完全正常** -> 问题是**这几个 GPIO 没有引到物理脚 17/19/20**
- 结论: `PRODUCTION.md` 里 "物理17/19/20 -> gpio 40/56/57" 的映射**有误**
- 已确认可用的物理脚组: **14/15/16** (gpio 49/50/51), **4/5/9**, **10/11/12**

### 可用 GPIO 普查 (2026-09-22, 排除电机占用的13个)
```
0 1 2 3 4 5 6 32 34 35 36 64 65 66 67 68 69 70 71 72 73 121 122 123
```
共 24 个可正常导出并翻转。**但物理脚位置未知** -> 需查官方 40pin 引脚图。

### 待办
- [ ] 确认物理脚 17/19/20 的真实 GPIO 号 (看板子丝印, 或查官方引脚图)
- [ ] 给 C 通道(左前轮)改接到其它可用脚, 恢复四轮


- 每路需 PWM+IN1+IN2 (共 12 GPIO); 转向反了就对调该通道 IN1/IN2 (改 car_config.json, 不用改线)
- 驱动板 GND 必须与板子共地 (3/8/13 脚任意)
- **ADC 电压检测 (物理32脚 → SARADC_IN1)【✅ 已通】**
  - 读数: `/sys/bus/iio/devices/iio:device0/in_voltage1_raw` (794 ≈ 1.39V)
  - 电池电压 = ADC毫伏 × 分压比 (`car_config.json → telemetry.adc_ratio`, 万用表标定值 **8.336**)
  - 标定依据: 万用表 11.69V 对网页 12.20V -> 8.7 × 11.69/12.20 = 8.336
## 3. EC801ECNCC Cat1 → USB host【⚠️ SIM 已通, 但不能上网 - 内核模块不兼容】
1. USB 默认 device 模式, 已通过 DT overlay 切 **host**（见下文持久化）
2. 模块枚举: `2c7c:0903 EC801E-CN`, 5 个接口:
   - iface 0: CDC 控制 / **iface 1: CDC-ECM 网卡数据 (class=0x0a)** —— 驱动缺失, 见下
   - iface 3: AT 口 (bulk out 0x0b / in 0x82)
3. AT 测试工具: 板上 `/root/usbat2`（usbfs 用户态, 无需内核驱动）
   `/root/usbat2 2c7c:0903 ATI`
   注: usbat2 响应会错位; 更可靠的是自写的纯 Python usbfs 客户端 `_atclient3.py`
   (**要点: ARMv7 的 USBDEVFS_BULK ioctl = 0xC0105502, 非 x86_64 的 0xC0185502**)

### ✅ SIM 卡测试结果 (2026-09-11, PIN 84602971)
| 项目 | 结果 |
|------|------|
| `ATI` | Quectel EC801E, Rev EC801ECNCGR07A03M02 |
| `AT+CPIN?` | 初始 `SIM PIN` → 输入 PIN 后 `READY` ✅ |
| `AT+ICCID` | 898600140624F5093270 ✅ |
| `AT+COPS?` | **CHINA MOBILE** ✅ |
| `AT+CREG?`/`AT+CEREG?` | `0,1` **已注册** ✅ |
| `AT+CGATT?` | `1` 已附着 ✅ |
| `AT+CSQ` | 20~27 信号良好 ✅ |
| `AT+CGDCONT` + `AT+CGACT` | 激活成功, `+CGPADDR: 10.52.13.244` ✅ |

**结论: SIM 卡、模组、注册、PDP 全部正常, 已拿到运营商 IP。**

### 🔴 剩余唯一阻塞: 内核无 ECM 网卡驱动 (且预编译模块不兼容)
- 板上有预编译模块 `/userdata/kmods2/` 与 `/root/kmods/`:
  `usbnet.ko` `cdc_ether.ko` `rndis_host.ko` `option.ko` `qmi_wwan.ko` `cdc-wdm.ko`
- **但 `insmod usbnet.ko` 直接 kernel Oops**:
  ```
  Unable to handle kernel paging request at virtual address 5f018af5
  Internal error: Oops - BUG: 5 [#1] THUMB2
  Modules linked in: usbnet(+) ...
  [<b0042a10>] (load_module) from [<b0042e47>] (sys_finit_module+0x53/0x5c)
  ```
- vermagic 字符串**表面匹配**(`5.10.160 mod_unload ARMv7 thumb2 p2v8`), 但崩溃点在
  `load_module` 内部 -> 典型的**结构体布局/modversion CRC 不匹配**
  (`usbnet.ko` 引用 `__LINK_STATE_*` `__QUEUE_STATE_*` 等内核内部符号, 随构建变化)
- ⚠️ **副作用: 加载失败后 `/proc/modules` 读不出内容(0 行), 内核模块链表已损坏, 需重启板子**
- `usb0` (172.32.0.70) **不是**模组网卡, 是板子自身的 USB gadget (UDC `ffb00000.usb`), 与 4G 无关

### 要真正上网, 二选一
1. **用与当前内核精确匹配的源码重新编译**这 6 个模块
   (内核 5.10.160 #22, RV1106 luckfox SDK; 拿到同版本 SDK 后 `make modules`)
2. **换 luckfox 官方带 4G 驱动的固件** (最省事)

### 📌 2026-09-11 实测记录: 自己编译模块**失败**, 原因已查明
#### 已确认的事实
- 官方**没有**"带 4G 驱动"的固件 —— 4G 需从 SDK 重编内核
  (参考 [Luckfox SDK 镜像编译](https://wiki.luckfox.com/zh/Luckfox-Pico-RV1106/SDK/SDK-Image-Compilation/))
- 现有 `Luckfox_Pico_Pro_Max_Flash_250607` 是**通用固件**, 其 defconfig 里
  `# CONFIG_USB_NET_DRIVERS is not set` —— 刷它**不解决 4G**, 且会清空全部配置
- 服务器 `~/luckfox-pico` SDK 处于 **detached HEAD e2b0ffa22 (2025-06-11 10:47)**,
  与板子内核构建日 (2025-06-11 18:43) **同日**, 源码基线一致
- SDK 自带**完全匹配的工具链**: `tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin`
  → `gcc 8.3.0 (crosstool-NG 1.24.0)`, 与板子 `/proc/version` 完全一致

#### 已排除的原因 (逐个实测否定)
| 假设 | 做法 | 结果 |
|------|------|------|
| 工具链不匹配 (13.3.0 vs 8.3.0) | 换用 SDK 自带 8.3.0 重编 | ❌ 仍 SIGSEGV(139) |
| DEBUG_INFO 体积干扰 | 关闭 DEBUG_INFO + strip (427K→29K) | ❌ 仍 SIGSEGV(139) |
| 缺父级菜单开关 | 补 `CONFIG_USB_NET_DRIVERS=m` | 配好了, 但仍崩溃 |
| 符号缺失 | 核对 14 个核心符号 | 全部已导出 ✓ |
| 重定位类型异常 | readelf 查 ELF | 全部标准 ARM 重定位 ✓ |
| vermagic 不符 | 对比字符串 | 完全相同 |

#### 结论
内核 `#22` 与 SDK 当前源码虽同日, 但**出厂固件用了不同的 .config**
(SDK 默认 `luckfox_rv1106_linux_defconfig` 明确关闭 `CONFIG_USB_NET_DRIVERS`)。
`.config` 差异 → 结构体布局不同 → `load_module` 内崩溃。
**必须拿到出厂固件对应的 .config 才能编译出可加载的模块。**

#### 下一步可行方案 (按推荐度)
1. **在 SDK 里把 USB_NET 配置编进内核, 整体重刷自制固件**(最彻底)
   - `./scripts/config --module USB_NET_DRIVERS ...` 后 `./build.sh`
   - 会**清空板子数据**, 但有完整备份 (见下)
2. **向 luckfox 官方索取出厂固件对应的 kernel config**
3. 暂缓 4G, 继续用有线网 (当前车已能跑)

#### ⚠️ 重要提醒
- `insmod` 这几个模块**会导致内核模块链表损坏**(`/proc/modules` 读不出), **必须重启**恢复
- 已重启 3 次恢复; 板子当前健康 (11 模块, 4 服务正常)
- 备份位置: `luckfox-flash/board_backup_20260911_2050/` (17 个文件: 全部脚本/配置/网页/文档)
- 编译产物留在服务器: `~/luckfox-pico/sysdrv/source/kernel/drivers/net/usb/*.ko`

- 4 路电机需要: 每路 PWM+IN1+IN2 (共 12 个 GPIO) + STBY, 用 PWM 时注意 DTB 里 pwm 节点是否启用

## 4. GPS (PG-131R)【⚠️ 需改线到 6/7 脚】
- **21/22 脚无法做串口**（GPIO0_A4/A5 无 uart 复用, 已查 SDK pinctrl）, 原接线无效
- 改接: GPS TXD → **物理7脚**(GPIO1_C4/uart4_RX), GPS RXD → **物理6脚**(GPIO1_C5/uart4_TX), VCC→3V3(36), GND→任一GND
- uart4 已启用: 设备 `/dev/ttyS4`, 9600 NMEA, 控制器后台线程自动解析 GGA/RMC
- 验证: `stty -F /dev/ttyS4 9600 raw; cat /dev/ttyS4` 应看到 $GxRMC/$GxGGA

## 5. 供电
- 板子: Type-C 5V（或电池经稳压）
- 电机: 独立电池经 TB6612 供电

## 硬件配置持久化机制【重要】
板子 uboot 加载的 DTB 无法通过改 mtd3 替换（FIT 校验, 已验证无效, 勿再尝试）。
当前采用 **内核 configfs overlay** 方案:
- overlay 文件: `/userdata/hwcfg/hw.dtbo`（启用 uart4 + usb host）
- 开机应用: `/etc/init.d/S96hwcfg`（含 dwc3 重绑）
- 改法: 编辑 `_uart5.dts` → 板上 `dtc -@ -I dts -O dtb -o /userdata/hwcfg/hw.dtbo`

## 控制端 / 网页【✅ 已更新】
- 网页: `/userdata/car/index.html` (本地直接打开)
- MQTT: `ws://<broker-host>:8083/mqtt`, 用户 car / <见 car_config.json>, token: <见 car_config.json>
- 状态消息 `luckfox/car/status` 新增 `tel` 字段:
  - `bat_mv`/`bat_v`: 电池电压 (ADC×ratio)
  - `gps`: {present, fix, lat, lon, sats, speed_kmh}
  - `net4g`: {present, module}
- 控制器: `/userdata/car/car_controller.py`, 日志 `/tmp/luckfox-car.log`

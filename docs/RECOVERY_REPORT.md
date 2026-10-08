# Luckfox Pico Pro Max 恢复报告

> 日期: 2026-10-08 (第二次刷机重建)
> 板子: Luckfox Pico Pro Max (RV1106), 内核 5.10.160, 固件 250607
> 当前 IP: **192.168.3.87** (网口, DHCP 分配, 会变!) / **192.168.3.69** (经 C5 WiFi, 稳定)
> **管理入口优先用 .69 隧道侧**; 网口 IP 以实际为准 (扫网段或看路由器)

## 〇-2、隧道 C 化完成 (2026-10-08) ✅

**spinet.py 已被 `spinet_c`（C 用户态泵）替代**，收益实测：

| 指标 | Python | C |
|---|---|---|
| 图传中隧道 CPU | 17-25% | **6-8%** |
| 空闲 CPU | 8% | ~1% |
| 推流帧率 | 200-290 fps | **340 fps**（每帧开销小） |
| fails/retx | 0 | 0 |

* 源码: `spinet_c/spinet.c`（spinet.py 的 1:1 移植，协议细节全保留：
  滑动窗口/重传/打包/空闲退避/learn_peer/默认路由跟随网线/udhcpc reaper）
* 编译: WSL `arm-linux-gnueabihf-gcc -O2 -Wall -static`（apt 装的 gnueabihf，
  静态链接不依赖板上 uClibc），产物 504KB
* 部署: `/userdata/car/spinet_c`；S22spinet 优先启动 C 版（无则回退
  spinet.py），S24 看门狗匹配两者
* 回滚: 把 spinet_c 改名/删掉再重启即可回到 Python 版
* 内核版 spitun.ko 仍是搁置状态（C 用户态已拿到几乎全部收益，风险却小一个量级）

### web_server 也抠了一把 (2026-10-08)
实测真凶不是 ADC（过采样早已从 400 降到 41，只剩 12.6ms），而是
**sys_stats.sample() 单次 198.7ms**——detect_info/processes/thread_cpu
三个全 /proc 扫描被 1Hz 调用。改为轻重分离：cpu/mem/load/temp 保持 1Hz，
disk/npu/detect/proc/threads 缓存 5s（JSON 结构不变，面板看不出差别）。
**web_server 空闲 CPU 15% → 6%**。全机空闲态占用 ~20% → ~10%。

---

## 〇-1、切内核事故与全量重建 (2026-10-08 凌晨)

### 事故
在线切换 spitun.ko (内核版隧道) 时, 运行中 insmod 把内核搞挂, 板子失联。
**教训: 内核模块的加载/overlay 切换只能在重启边界做, 且该模块是针对 C3 时代
固件验证的, 对 C5 未验证。此方向已搁置, 除非先用串口验证兼容性。**

### 刷机恢复 (PC 命令行, 无需 SocToolKit 界面)
```bash
# 板子 USB 连电脑, MaskRom 模式 (boot-loop 状态自动就在)
cd SocToolKit/SocToolKit_v1.98_20240705_01_win/bin/windows
./upgrade_tool.exe LD                       # 应看到 Mode=Maskrom
./upgrade_tool.exe uf "...\Luckfox_Pico_Pro_Max_Flash_250607\update.img"
```

### 原厂固件要点 (250607)
* **没有 sshd init 脚本** (但 /usr/sbin/sshd 二进制在): adb 进去启动,
  重建脚本会装 /etc/init.d/S50sshd
* **/root 属主是 1005** → sshd StrictModes 拒收 authorized_keys,
  必须 `chown root:root /root`
* adb 通道: `platform-tools/adb.exe shell` (板子 USB 连电脑即可用, 救命通道)
* 刷机后网口 DHCP 地址会变 (.86 → .72 → .87), 用 .69 隧道侧最稳

### 一键重建
`bash _postflash_restore.sh <网口IP>` — 自动完成:
car 文件 / init 脚本 (S21-S26, 含竞态修复版 S25rkipc) / mediamtx+hls 关 /
NPU 模型+PET rkipc / H264+GOP12+enable_npu / paho / sshd 自启
**注意还需手动补**: `/userdata/hwcfg/spi0_spidev.dts`+编译 + `/userdata/hwcfg/tun.ko`
(见 `_fix_tunnel.sh`; 原因: 这两个文件在 /userdata, 会被刷机清掉, 已加入下次清单)

### 重建后验证 (全部通过)
H264 720p 25fps ✓ NPU load 40% ✓ 隧道 fails=0 ✓ 图传 25.5fps ✓
spinet 空闲退避 ✓ index.html 断线退避 ✓ 单 spinet 进程 ✓

---

## 〇、CPU 优化 + NAPT 死锁事故 (2026-10-07 深夜)

### CPU 优化（已部署、已持久化）
| 项 | 改动 | 效果 |
|---|---|---|
| spinet.py 空闲退避 | 空闲 sleep 从固定 1ms 改为阶梯 1→4→8ms（有活立即全速） | 空载 17% → 8% |
| mediamtx | `hlsAlwaysRemux: yes` → `no`（网页只用 WHEP, HLS 无观众也在 24h 转封装） | 空载 8% → 0% |
| index.html 断线退避 | poll 1s / WHEP 2s 固定重试 → 失败后指数退避（≤8s / ≤5s）, 成功清零 | 根治 NAPT 死锁(见下) |

图传中稳态: 总占用 ~59% (空闲 41%), 视频 25fps, fails=0。图传中的 CPU 大头是
spinet.py 的 Python 逐包转发 (~25%), 这是架构决定的; 再往下要 C 重写泵循环。

### ⚠️ NAPT 死锁事故（本轮最重要教训）
**症状**: spinet 重启（隧道闪断 ~25s）后, PC 到板子的**所有 TCP**（.69 的 22/80/8889
和网口 .86 的全部服务）永远连不上, 但 SPI 两个方向都健康、C5 maps=6 端口映射都在。

**根因链**:
1. 隧道闪断期间, 打开的页面开始疯狂重试（poll 每 1s + WHEP 每 2s, 各带 SYN + ICE-TCP）
2. 每次重试在 **C5 的 NAPT 连接表**里制造半开 TCP 条目; 正向转发还能过（SYN 能到板子）,
   但**回程改写要查表**, 表满 → SYN-ACK 不改写/丢弃 → PC 收不到 → 页面继续重试 → 表永远满
3. 自维持死锁: 页面的重试本身就是"病因维持者"

**急救步骤**（比拔电快）:
1. **关掉/切走所有打开的小车页面**（停止重试源）—— 受控标签页直接 `goto about:blank`
2. 等 2~3 分钟让 C5 的 NAPT 表排空
3. 端口即恢复（实测 22/80/8889 全部 OPEN）, 重新打开页面即可

**预防**: index.html 的 poll/WHEP 重试已加退避（断线时最多 8s/5s 一次）,
断线风暴不再可能灌满表。

**诊断手段（下次直接用）**:
* C5 健康: PC 发 UDP `stat` 到 `192.168.3.69:10000` → JSON（堆/任务/up_ms）
* C5 转发计数: 板子(网口 .86)监听 UDP 9999 收 C5 每 10s 的广播状态行
  （含 `ip tx/rx/drop maps=` —— maps=6 说明端口映射在）
* 判定分水岭: 板子上 `python3` 绑 8889 收 SYN —— 收到=C5 正向转发好, 问题在回程

---

## 一、最终架构

```
手机/PC ──WiFi──> ESP32-C5 (192.168.3.69)
                     │
                     │ SPI (20MHz, 物理脚 12/14/15/16)
                     ↓
              Luckfox RV1106
              ├── /dev/spidev0.0  ← spinet.py
              ├── /dev/net/tun    ← tun0 (10.77.0.2 ↔ 10.77.0.1)
              ├── web_server.py   :80
              └── eth0 (192.168.3.86) 备用有线通道
```

## 二、SPI 接线（已实测验证）

| Luckfox 物理脚 | GPIO | 信号 | C5 GPIO |
|---------------|------|------|---------|
| 14 | 49 | CLK | 6 |
| 15 | 50 | MOSI | 7 |
| 16 | 51 | MISO | 2 |
| 12 | 48 | CS | 10 |
| 13/18 | — | GND | GND |

**必须共地。** 验证结果: `fails=0 retx=0`，287 帧/秒。

## 三、开机自启（已重启验证）

| 脚本 | 作用 |
|------|------|
| `/etc/init.d/S21spitun` | configfs 挂载 + SPI overlay + tun.ko |
| `/etc/init.d/S22spinet` | 启动 spinet.py（等 /dev/spidev0.0） |
| `/etc/init.d/S23web` | 启动 web_server.py :80 |
| `/etc/init.d/S24spinet_wd` | spinet 看门狗（20s 间隔） |
| `/etc/init.d/S25rkipc` | 摄像头（加载 sc3336.ko，检测到 sensor 才起 rkipc） |

依赖文件（`/userdata/hwcfg/`，ubifs 持久）：
- `spi0_spidev.dts` / `.dtbo` — SPI overlay
- `tun.ko` — TUN 模块

### 重启验证结果（多次实测）

```
uptime 89s 时:
  /dev/spidev0.0  ✅  /dev/net/tun  ✅
  SPI overlay     ✅ spi0 = okay
  tun 模块        ✅
  spinet.py       ✅ 运行中
  web_server.py   ✅ :80 监听
  看门狗          ✅ 运行中
  sc3336 模块     ✅ 已加载
  隧道            ✅ 293 帧/秒, fails=0 retx=0 drop=0
  外网            ✅ ping 8.8.8.8 通
  HTTP            ✅ 200
```

## 四、⚠️ 关键教训（用两次刷机换来的）

### 1. 开机脚本**绝对禁止同步阻塞命令**

`rcS` 串行执行 `S*`。任何脚本卡住 → 后面的 `S40network`/`S50sshd`/`S99*`
**全都不执行**。表现：

- ✅ ping 通（网络栈在内核里）
- ✅ RTSP 通（rkipc 由 S21appinit 启动）
- ❌ **SSH / Telnet / Web 全死**
- → 只能重新刷机

**规则**：所有 `insmod` 必须放后台子 shell：
```sh
( insmod "$KO" > /tmp/load.log 2>&1; echo "RC=$?" >> /tmp/load.log ) &
```

### 2. SPI 速度：24MHz 跑不动，20MHz 可以

| 速度 | 结果 |
|------|------|
| 24MHz（原配置） | magic 全错，无法通信 |
| 1MHz | 能通，但只 29 帧/秒 |
| **20MHz** | ✅ **287 帧/秒，零错误** |

改速度：`echo 20000000 > /userdata/spi_speed` 然后重启 spinet。

### 3. overlay 机制四个坑

1. **两步操作**：`cat dtbo > .../dtbo` **然后** `echo 1 > .../status`
2. **不能写 `&symbol`**：板上 dtc 解析不了，会编成 `0xffffffff`。用
   `target-path` 绝对路径 + **数字 phandle**（从 `/proc/device-tree/pinctrl/<外设>/<组>/phandle` 读）
3. **`pinctrl-names` 必须是 `"active"`**（PWM 情况），写 `"default"` 会报
   `No active pinctrl state`
4. **configfs 在 S22 阶段还没挂**（只有 S50usbdevice 挂），脚本必须自己
   `mount -t configfs none /sys/kernel/config`

### 4. `spitun.ko` vs `spinet.py` —— 两套不同实现

| | `spitun.ko`（内核模块） | `spinet.py`（用户态）**← 本项目用这个** |
|---|---|---|
| SPI 接口 | 独占，需**禁用** spidev | `/dev/spidev0.0`，需**启用** spidev |
| 网络接口 | netdev (`spitun0`) | TUN (`tun0`) |
| 状态 | ❌ netdev xmit 有 `skb->len=0` bug | ✅ 成熟可用 |
| 卸载 | ❌ `exit` 有 `free_netdev` double-free，rmmod 会崩 | ✅ 正常 |

**别混淆这两个。** overlay 里 spidev 的状态取决于用哪个。

### 6. 开机脚本里的两个隐蔽陷阱（第二次踩到）

**(a) `( cmd ) &` 在 rcS 阶段不可靠**
第一版 `S25rkipc` 用 `( do_start ) >/dev/null 2>&1 &` 派发后台任务，
结果开机后 `sc3336` 模块**没加载**、`rkipc` 也没起来 ——
父 shell 退出时子 shell 的工作被连带回收。
**改用 `setsid`**（和 `S23web`/`S24spinet_wd` 一致）：
```sh
setsid "$WORKER" </dev/null >/dev/null 2>&1 &
```

**(b) `ps | grep '[x]xxx'` 会误匹配**
`S25rkipc status` 里用 `ps | grep '[r]kipc'` 判断进程是否在跑，
但当启动命令本身含 "rkipc" 字符串时（如
`sh -c "... /etc/init.d/S25rkipc status ..."`），**那个 shell 自身也被匹配到**，
造成"rkipc 已在运行"的假象，掩盖了真实故障。
**改用 `/proc/<pid>/comm` 精确匹配**：
```sh
for p in $(ls /proc | grep -E '^[0-9]+$'); do
    [ "$(cat /proc/$p/comm)" = "rkipc" ] && return 0
done
```

### 7. `spinet.py` 的 `learn_peer` 会劫持管理通道

浏览器从 `192.168.3.64` 经隧道访问时，spinet 会自动加
`192.168.3.64 dev tun0` 主机路由（让回包走隧道）。
**副作用**：PC 从网口 `.86` 的 SSH/HTTP 会被劫持而断掉。

- `peer_idle = 120` 秒后自动撤回
- 管理建议走 `.69`（隧道侧），或等 120s

## 五、图传已打通（2026-10-07 晚，本轮完成）✅

### 1. 摄像头恢复
SC3336 在 I2C 上恢复应答（0x30 = UU，驱动已认领）——CSI 排线/供电的物理问题已解决。
`dmesg` 可见 `dphy0 matches m00_b_sc3336`、`rkisp queue buf done`。

### 2. 缺失的 mediamtx（重刷机弄丢的）已重新部署
- 二进制: `/root/mediamtx/mediamtx`（v1.11.3，来自 PC 的 `mediamtx_armv7.tar.gz`）
- 配置: `/root/mediamtx/mediamtx.yml`（= 仓库 `car/mediamtx_min.yml`，WHEP 8889 + ICE-TCP 8189）
- 自启: `/etc/init.d/S26mediamtx`（= 仓库 `car/S25mediamtx`，排在 S25rkipc 之后）
- 注意: busybox tar **不支持 -z**，先在 PC 解成纯 tar 再传

### 3. 编码格式改回 H.264（浏览器 WebRTC 不认 H.265，重刷机后回到了 H.265 默认）
```sh
# 两处都要改（开机时 RkLunch.sh 会用模板覆盖 /userdata/rkipc.ini）:
sed -i 's/output_data_type = H.265/output_data_type = H.264/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
sed -i 's/^gop = 50/gop = 12/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
# 然后切流畅档（会用 video_ctl 安全重启 rkipc）:
cd /userdata/car && python3 -c "import video_ctl; print(video_ctl.video_switch('smooth'))"
```
备份: `/userdata/rkipc.ini.h265bak`

### 4. rkipc 双进程竞态（本轮新发现，已修）
S21appinit→RkLunch.sh 和 S25rkipc **都会**起 rkipc。摄像头正常时两个都起，
后到的 bind 554 失败但不退出，白占 ~20MB（之前摄像头坏所以只见过单进程）。
修复: `S25rkipc` 的 worker 改为**最多等 20 秒**，期间发现 rkipc 出现就让位
（`car/S25rkipc` 已同步）。重启验证: rkipc 总数 = 1。

### 5. 最终验证数据
| 项目 | 结果 |
|------|------|
| 流参数 | H264 · 1280x720 · 25fps · ~1.4Mbps（ffprobe 经 HLS 8888 实测） |
| 浏览器播放 | WebRTC/WHEP 经 C5 .69:8889 → ICE-TCP :8189，**24.8~26 fps**（requestVideoFrameCallback 实测） |
| 重启自愈 | 断电重启后浏览器页面自动重连恢复播放；服务全部自启 |
| 负载 | loadavg ~12 但 CPU 50% 空闲 —— 是 D 状态媒体内核线程（venc/rkisp/vpss）计入 loadavg，**属正常现象，不是故障** |

### 6. 遗留小项
- `i2cdetect` 里 0x30 显示 UU（驱动占用，正常）
- HLS part duration 警告（240ms vs 201ms）只影响 iOS 客户端，可忽略

## 五-旧、摄像头硬件排查记录（已解决，留档）

**结论：SC3336 传感器在 I2C 上零应答，主机侧完全正常。**

#### 全部排查记录

| 检查 | 结果 |
|------|------|
| 设备树 `/i2c@ff470000/sc3336@30` | ✅ `status=okay`，`compatible=smartsens,sc3336` |
| 驱动 `sc3336.ko` | ✅ 加载成功，`driver version: 00.01.01` |
| 驱动卸载→重载 | ✅ 试过，结果相同 |
| PWDN 电源脚（pin 21） | ✅ 手动低→高→低复位时序，**无效** |
| I2C 控制器 `ff470000.i2c` | ✅ probe 成功，时钟 198MHz |
| I2C 引脚 119/120 复用 | ✅ 正确绑到 `i2c4m2-xfer` |
| **`i2cget -y 4 0x30 0x00`** | ❌ **`No such device or address` (ENXIO)** |
| **`i2cset -y 4 0x30 0x00 0x00`** | ❌ ENXIO |
| **`i2cdetect -y 4`** | ❌ **所有地址无应答** |
| rkisp / rkcif / MIPI DPHY | ✅ 全部 probe 成功，等 sensor |

#### 关键日志

```
sc3336 4-0030: Unexpected sensor id(000000), ret(-5)   ← 读寄存器全零
i2cget: read failed: No such device or address         ← 从机不应答
```

**I2C 主机侧工作正常（控制器/时钟/引脚复用都对），但从机在 0x30 完全不 ACK。**
`sensor id(000000)` + ENXIO = 芯片没有上电或没有物理连接。

#### 需要物理检查

1. **CSI 排线**：两端卡扣扣紧、金手指对准、**方向没插反**
2. **排线是否折断**（重新接线时可能压到）
3. **摄像头模块供电**

#### 接好后一键恢复

```sh
sh /userdata/car/cam_up.sh
```

该脚本会：检测 I2C→加载驱动→启动 rkipc→验证 RTSP，并给出明确结论。
成功时打印 `rtsp://<ip>:554/live/1`。

## 六、网络质量实测

| 目标 | 丢包 | 说明 |
|------|------|------|
| ping C5（隧道零跳，10.77.0.1） | **0%** | ✅ SPI 隧道完美 |
| ping 路由器（经隧道一跳） | **0%** | ✅ 转发正常 |
| ping 8.8.8.8（外网） | 0~40% 波动 | ⚠️ 上游 WiFi/ISP 问题，**与隧道无关** |

隧道统计恒为 `fails=0 retx=0 drop=0`，~290 帧/秒。

启动命令（摄像头好了之后）：
```sh
insmod /oem/usr/ko/sc3336.ko
LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib \
  /oem/usr/bin/rkipc -a /oem/usr/share/iqfiles &
```

## 六、paho-mqtt（已完成）

板子没有 pip，用**离线 wheel** 安装：
```sh
# PC 下载 paho_mqtt-2.1.0-py3-none-any.whl, 解压出 paho/ 目录
# tar -cf paho.tar paho paho_mqtt-*.dist-info   (BusyBox tar 不支持 -z)
# 板上: tar -xf /tmp/paho.tar -C /usr/lib/python3.11/site-packages
```

验证：`python3 -c "import paho.mqtt.client"` OK

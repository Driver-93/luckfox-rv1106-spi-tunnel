# Luckfox 4G 遥控小车 — 生产环境说明

> 生成: 2026-09-11  状态: 全链路调通(图传/遥控/GPS/电压遥测)
> 本文档为生产环境的最终配置与运维手册, 排障先看本文最后两节。

## 1. 系统组成

| 节点 | 地址 | 访问方式 |
|------|------|----------|
| 小车主板 | Luckfox Pico Pro Max (RV1106, 720P H264) | `ssh -i luckfox-flash/id_ed25519 root@192.168.3.67` |
| 云服务器 | `YOUR_SERVER_IP` (Ubuntu 24.04) | `ssh -i luckfox-flash/server_car.pem ubuntu@YOUR_SERVER_IP` |
| 控制网页 | http://YOUR_SERVER_IP:8082/ | 任意浏览器(手机/PC) |
| MQTT | `YOUR_SERVER_IP:1883` (EMQX) | ws://YOUR_SERVER_IP:8083/mqtt, 用户 `car`/`YOUR_MQTT_PASSWORD`, 指令需带 token `YOUR_API_TOKEN` |

⚠️ 板子 IP 由 DHCP 分配, 目前 192.168.3.67 (原 .66); IP 变了先 ping/扫网段。

## 2. 供电与开机自启 (上电即全自动)

电池 3S → 驱动板 VIN; 板子由驱动板 5V 或独立 5V 供电。
开机自启脚本 (/etc/init.d/):

| 脚本 | 作用 |
|------|------|
| S96hwcfg | 应用设备树 overlay (uart4 + USB host), dtbo 在 /userdata/hwcfg/hw.dtbo |
| S97relay | 图传推流看门狗 → /userdata/car/relay_loop.sh |
| S98mediamtx | 板端 mediaMTX (局域网 WebRTC 直连), 程序在 /root/mediamtx/ |
| S99car | 小车控制器 car_controller.py (MQTT + 遥测 + GPS 线程) |

工厂脚本 RkLunch.sh 会在每次开机用 /oem/usr/share/rkipc-300w.ini
**覆盖** /userdata/rkipc.ini —— 该出厂文件已被我们改为 720P H264 参数 (勿再改回)。

## 3. 图传链路 (720P H264, WebRTC)

```
摄像头 SC3336 → rkipc RTSP (127.0.0.1:554/live/0, 1280x720 H264 High 25fps CBR 1.5M, GOP 1s)
   ├─ 局域网: 板端 mediaMTX (192.168.3.67:8889) → WebRTC 直连 (~100-200ms)
   └─ 外网:   board ffmpeg → rtsp://YOUR_SERVER_IP:8554/car → 服务器 mediaMTX → WebRTC (~400-700ms)
```

- 网页自动按 板子直连 → 服务器中继 顺序尝试, 断线 5 秒重连
- 推流看门狗: relay_loop.sh 每 5 分钟强制重连一次, 防僵死连接 (掉线自愈)
- 服务器 cartrans 转码服务已停用 (摄像头直出 H264, 无需转码)
- 服务器 mediamtx 配置 /etc/mediamtx/mediamtx.yml (WebRTC :8889, HLS :8081, API :9997)

## 4. 电机接线 (TB6612 四路 MD240A, 布局: C=左前 D=左后 A=右前 B=右后)

| 通道 | 驱动板信号 | 板子物理脚 | GPIO号 | 配置项 |
|------|-----------|-----------|--------|--------|
| C 左前 | CIN1 / CIN2 / PWMC | 14 / 15 / 16 | 50/49/51 | FL (IN1↔IN2 已对调过方向) |
| D 左后 | DIN1 / DIN2 / PWMD | 17 / 19 / 20 | 40/56/57 | BL |
| A 右前 | AIN1 / AIN2 / PWMA | 4 / 5 / 9 | 55/54/58 | FR |
| B 右后 | BIN1 / BIN2 / PWMB | 10 / 11 / 12 | 59/41/48 | BR |
| STBY | STBY | 34 | 7 | pin.STBY |

- 驱动板 GND 必须与板子 GND 共地 (3/8/13 脚任意)
- 电池 + → 驱动板 VIN (5.5~15V)
- 编码器脚 EXA/EXB 未使用
- 电机转向若反: 对调该通道配置里的 IN1/IN2 (car_config.json), 无需改线

## 5. GPS (PG-131R, uart4)

| GPS | 板子物理脚 |
|-----|-----------|
| TXD | 7 脚 (uart4 RX, GPIO1_C4) |
| RXD | 6 脚 (uart4 TX, GPIO1_C5) |
| PPS | 26 脚 (GPIO0_A2, 备用) |
| VCC/GND | 36 脚 3V3 / GND |

- 9600 NMEA, 控制器后台线程解析 GGA/RMC → status 消息 tel.gps
- ⚠️ 官网引脚图的 21/22 "UART1_TX/RX_M1" 标注是错的 (GPIO0 无 uart 收发复用), 别接那
- 定位需室外可见天空, 冷启动 1~5 分钟

## 6. 电池遥测 (3S 锂电)

- 驱动板 ADC 输出 → 板子 32 脚 (SARADC_IN1)
- 换算: 电压 = raw × 1.7578mV × **8.34** (adc_ratio, 已用万用表标定: 万用表 11.69V 对网页 12.20V, raw≈798)
- 网页阈值: <10.5V 红 / 10.5~12V 黄 / ≥12V 绿 (3S)

## 7. 文件位置

| 位置 | 内容 |
|------|------|
| 板 /userdata/car/ | car_controller.py, car_motor.py, index.html, car_config.json, relay_loop.sh |
| 板 /etc/luckfox-car/car_config.json | → 软链到 /userdata/car/car_config.json |
| 板 /root/ffmpeg | armhf ffmpeg (推流用) |
| 板 /root/mediamtx/ | 板端 mediaMTX + mediamtx.yml |
| 板 /oem/usr/share/rkipc-300w.ini | 摄像头参数源文件 (已改 720P H264) |
| 服务器 /var/www/luckfox-car/ | 控制网页 (nginx :8082) |
| PC luckfox-flash/car/ | 全部源码本地副本 (与板同步) |

## 8. 常用运维命令

```bash
# 板上: 看图传/服务状态
ssh root@192.168.3.67 "pidof rkipc ffmpeg mediamtx python3; tail -3 /tmp/relay.log"
# 重启推流
ssh root@192.168.3.67 "killall -9 ffmpeg; setsid /userdata/car/relay_loop.sh &"
# 重启摄像头 (注意先 kill -9 防止它回写旧配置)
ssh root@192.168.3.67 "kill -9 \$(pidof rkipc); sleep 1; LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib setsid /oem/usr/bin/rkipc -a /oem/usr/share/iqfiles >/tmp/rkipc.log 2>&1 &"
# 服务器: 看 stream 状态
ssh ubuntu@YOUR_SERVER_IP "sudo journalctl -u mediamtx --no-pager -n 20"
# 部署网页 (改完本地 index.html 后)
scp luckfox-flash/car/index.html root@192.168.3.67:/userdata/car/index.html
scp luckfox-flash/car/index.html ubuntu@YOUR_SERVER_IP:/tmp/i.html   # 再 sudo cp 到 /var/www/luckfox-car/
```

## 9. 已知坑 (踩过的)

1. **RkLunch.sh 每次开机覆盖 rkipc.ini** → 必须改出厂源文件 `/oem/usr/share/rkipc-300w.ini`
   (RkLunch.sh:118 `cp $default_rkipc_ini $rkipc_ini -f` 是**无条件**覆盖, 因为第117行的
   `if [ ! -f "$rkipc_ini" ]` 判断**被注释掉了**)。只改 `/userdata/rkipc.ini` 重启即丢失。
   已改: 主码流 gop=12 / max_rate=1024 / mid_rate=768, 子码流 H.264 (备份 `.bak_orig`)
2. **rkipc 退出时回写内存配置** → 改它的配置必须先 `kill -9` 再改再启动
3. **官网引脚图 21/22 脚 UART1 标注错误** → GPS 用 6/7 脚 (uart4)
4. **uboot 的 FIT 校验** → 直接改 mtd3 boot 分区的 DTB 无效, 用 configfs overlay (S96hwcfg)
5. **服务器 2 核** → 避免 pm2 应用崩溃循环 (曾因 5000 端口冲突吃掉一半 CPU 导致图传 10 秒延迟); tagmap-backend 已改 5001 端口
6. **板子负载 avg 经常显示 8~10 但 CPU 空闲** → rkipc 线程 D 状态统计造成的虚高, 正常现象

## 10. 待办 / 未接

- [ ] Cat1 (EC801E) 已可枚举+AT (iface3), 上网需交叉编译 option/qmi_wwan 内核模块 (服务器有 arm-gcc)
- [ ] 电机编码器 (EXA/EXB) 未接, 当前开环控制
- [ ] 电池电压阈值可按实际使用再微调 (当前 10.5/12.0V)

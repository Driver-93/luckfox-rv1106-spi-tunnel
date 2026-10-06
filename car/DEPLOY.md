# Luckfox Pico MAX 4G 遥控小车 — 硬件清单 + 部署说明 (已确认淘宝采购)

## 硬件清单 (已确认)
| 硬件 | 型号 | 接口 | 说明 |
|------|------|------|------|
| 4G 模块 | EC801ECNCC 免驱 USB dongle (QTMDON0021DP-R) | USB 公头 | 免驱, 需 USB host 切换 |
| GPS 模块 | PG-131R-ROM + 15x15 有源天线 | UART 串口 | 定位, 需 TTL 串口接法 |
| 小车底盘 | 4WD 折弯板麦克纳姆轮双层 + TT马达 x4 | 4路 | 麦克纳姆轮 |
| 电机驱动 | TB6612 四路编码电机驱动模块 MD240A + 底板 | TB6612 x2 | 四路, 板载 3.3V/5V 稳压 + 霍尔编码器接口 |
| 摄像头 | SC3336 3MP Camera (B) 98.3° | CSI 排线 | RTSP 推流 |

## 软件栈 (已部署 /userdata/car/)
| 文件 | 用途 |
|------|------|
| car_motor.py | FourMotor 四路 TB6612 + 4WD 麦克纳姆轮运动模型 |
| car_controller.py | MQTT 控制守护 (收令→驱动电机→上报状态) |
| car_config.json | 配置 (broker/主题/引脚) |
| index.html | 控制端网页 (虚拟摇杆: 前后/原地转/横移) |
| S99luckfoxcar | init.d 开机自启 |

## MQTT 指令 (luckfox/car/cmd)
| c 值 | 动作 |
|------|------|
| forward | 前进 |
| backward | 后退 |
| spin_left / right | 原地左/右转 |
| strafe_left / right | 左/右横移 |
| stop | 停止 |
| brake | 刹车 |
```

## 引脚配置 (待接线)
编辑 `/etc/luckfox-car/car_config.json` 的 pin 段:
```json
"pin": {
  "STBY": 106,
  "FL": {"IN1":107,"IN2":108,"PWM":109},
  "FR": {"IN1":110,"IN2":111,"PWM":112},
  "BL": {"IN1":113,"IN2":114,"PWM":115},
  "BR": {"IN1":116,"IN2":117,"PWM":118}
}
```
默认是占位值(106-118), 接线后按实际 GPIO 编号修改。
MD240A 驱动板: 每个电机通道对应 TB6612 的 IN1/IN2/PWM 三根线, 共用 STBY。
(注意: 板载稳压+编码器接口, 若用编码器测速需额外接编码器信号线。)

## 进行中 (更新于摄像头装上后)
- [x] **SC3336 摄像头 → CSI → RTSP 推流 ✅ 已验证**
  - sc3336@30 设备树节点识别, sc3336.ko/ rkisp/ rkcif 已加载, rkipc 开机自启运行
  - RTSP 监听 554, 流地址 `rtsp://<板IP>:554/live/1`, H.265 子码流 704x576@25fps
  - 实测抓帧正常(亮度~103非黑屏), 流稳定
- [x] **网络打通 ✅** (本次通过 eth0 网线)
  - eth0 = 192.168.3.66/24 (DHCP from 192.168.3.1), 默认路由正常, 公网连通(能 ping YOUR_SERVER_IP)
  - 局域网 SSH: `ssh -i id_ed25519 root@192.168.3.66` 可达
  - 板子到 MQTT broker YOUR_SERVER_IP:1883 TCP 连通
- [x] **MQTT 控制链路 ✅ 已验证 (模拟模式)**
  - car_controller.py 连上 broker(账号 car), 每 5s 发心跳 `{"mode":"sim","dir":"stop","speed":0,"online":true}`
  - 从 broker 发 forward/spin_right/strafe_left/stop 均被正确接收并执行左右反向映射
  - daemon start: `cd /userdata/car && (nohup setsid env PYTHONPATH=/userdata/car python3 -u car_controller.py --simulate >/tmp/c.log 2>&1 </dev/null &)`
- [ ] 4 路电机 → 驱动 → GPIO → 真机测麦克纳姆轮
  - ⚠️ 已发现: 默认 pin 里 **GPIO 117/118 被内核占用(BUSY)**, 其余 11 个 ok → 需改配置或确认 mis5001(4-0031, I2C 0x31) 才是电机驱动而非 GPIO 的 TB6612
- [ ] GPS(串口) 定位
- [ ] USB host + EC801ECNCC 4G 联网
- [ ] 控制网页 + RTSP 实测

## 板子连接备忘 (当前)
- eth0 有线 = 192.168.3.66 (SSH 可用, 首选)
- USB ADB = f6b873fc9518adb1 (`adb shell`), USB 网卡 usb0 = 172.32.0.93/16
- RTSP 拉流: 有线时 `rtsp://192.168.3.66:554/live/1`; 仅 USB 时 `adb forward tcp:8554 tcp:554` + `rtsp://127.0.0.1:8554/live/1`
- 若 eth0 NO-CARRIER(carrier=0): 先 `ip link set eth0 up`, 若非物理问题通常恢复

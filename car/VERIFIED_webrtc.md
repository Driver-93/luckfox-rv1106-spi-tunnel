# 摄像头低延迟(WebRTC)预览 + 控制整合 - 已验证 ✅
时间: 摄像头装上后
延迟: 已根治转码瓶颈(见下)  WebRTC基准<200ms

## RV1103(64MB) C版移植 - 已完成源码+交叉编译+板上验证 ✅
- 起因: 30元 RV1103 64MB 撑不住 Python, 需纯C控制
- 产出: car/c/carctl.c (零第三方依赖) + car/c/build.sh + car/c/carctl_static(455KB静态arm)
- 特性: 手写MQTT v3.1.1客户端(无paho库) + /sys/class/gpio电机驱动 + token鉴权 + 心跳 + 断线重连
- 协议: 与 car_controller.py 完全兼容 (SUB luckfox/car/cmd, PUB luckfox/car/status)
- 编译: 腾讯云服务器装 gcc-arm-linux-gnueabihf 交叉编译 (x86_64 Ubuntu->arm)
- 板上验证(RV1106, 同架构): 连上broker, 心跳正常, 与Python版并存互不冲突
- 静态链接: RV1103 任何 Buildroot libc(musl/glibc/uclibc) 都能直接跑
- 内存: C版 ~10KB以下虚拟内存占用, 远优于Python~12MB
- 待办: 按实际接线改 carctl.c 顶部 PIN_* 宏

## 链路
板子RTSP(H.265) -> ffmpeg转H.264 -> 推RTSP到本地mediamtx(/car)
                 -> mediamtx WebRTC(WHEP) -> 浏览器实时播放(<200ms)

## 一键启动(仅视频预览)
  powershell -ExecutionPolicy Bypass -File start_webcam_webrtc.ps1
  自动: mediamtx + ffmpeg转码推流 + 打开 http://localhost:8889/car

## 图传+控制一体页面(整合完成)
  - 页面: http://localhost:8095/  (car\index.html, 由 serve_car.js 伺服)
  - 上方: WebRTC 低延迟实时画面(reader.js, WHEP http://localhost:8889/car/whep)
  - 下方: MQTT 虚拟摇杆(forward/backward/spin/strafe/stop + 速度)
  - MQTT: broker=YOUR_SERVER_IP:8083(mqtt/ws), 账号 car, cmd=luckfox/car/cmd
  - 启动: node serve_car.js 8095 C:\...\car  (index.html 用它伺服)

## 关键文件
  - car/index.html                图传+控制一体页
  - car/serve_car.js              伺服控制页 node服务器
  - car/start_webcam_webrtc.ps1   低延迟图传一键启动
  - car/mediamtx.yml              mediamtx配置(car path, publisher)
  - car/preview/hls_server.js     备用(HLS)服务器
  - car/start_camera_preview.ps1  备用(HLS)方案, 延迟高

## 验证
  - 浏览器CDP(单页同时): video readyState=4 paused=false 704x576 tracks=2(WebRTC)
    + MQTT已连接ws broker + 7个控制键
  - mediamtx日志: peer connection established + reading from path 'car', 1 track (H264)

## WebRTC地址
  本地 WHEP端点: http://localhost:8889/car/whep
  本地 播放页:   http://localhost:8889/car

## 公有云图传中转 - 已部署并验证 ✅
目标: 小车4G上网后, 手机从任意网络(wifi/4G)远程看画面
- 转推: 板子RTSP -> ffmpeg(本机或板上)转H.264 -> 推RTSP到腾讯云mediamtx car path
       推流地址 rtsp://YOUR_SERVER_IP:8554/car   (TCP)
- 公网播放页(低延迟WebRTC): http://YOUR_SERVER_IP:8889/car  (手机/任意设备可开)
- 云端mediamtx v1.20.1 已加 car path(source: publisher), 已重启生效
- 公网端口已验证可达: 8554(RTSP推流)/8889(WebRTC播放)/1935/8081 均开
- 日志证实: [RTSP] session is publishing to path 'car' (H264), [WebRTC] 读取car

### 你现在就能远程看的方式(验证中转链路)
  ffmpeg 把板子流转H.264推到云端car, 然后手机开 http://YOUR_SERVER_IP:8889/car
  转推命令(本机Windows):
    ffmpeg -rtsp_transport tcp -i rtsp://192.168.3.66:554/live/1 -an \
      -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 30 -b:v 1000k \
      -f rtsp -rtsp_transport tcp rtsp://YOUR_SERVER_IP:8554/car

### 最终4G方案(小车脱离电脑)
  4G联网后, 把上面"转推"放到板子上做(ffmpeg跑在板子,或板子rkipc直接RTMP/RTSP推到云),
  且控制走腾讯云EMQX(MQTT已在服务器上), 手机/网页公网访问即可。

## 公网访问清单
  实时画面(WebRTC): http://YOUR_SERVER_IP:8889/car
  控制侧: MQTT ws://YOUR_SERVER_IP:8083/mqtt (网页/APP)

## 公网远程控制页 - 已部署 ✅ (v6 官方reader.js + 延迟诊断)
- 地址: http://YOUR_SERVER_IP:8082/
- 图传: 官方 MediaMTXWebRTCReader (trickle ICE)
- 延迟显示 v6: 左上角【网络: xx · 画面: xx ms】
  - 网络 = RTCPeerConnection candidate-pair currentRoundTripTime (RTT)
  - 画面 = inbound-rtp jitterBufferDelay/jitterBufferEmittedCount (浏览器jitter buffer缓冲, ms)
  - HTTP兜底: 8秒无RTT则测网络往返
- 实测(headless): 网络:1295ms(环境噪声), 画面WebRTC正常; 真实数字需真机看

## 60秒画面延迟 - 根治(H.265转码瓶颈, 已消除) ✅
- 根因: 板子推H.265, 本机ffmpeg做 HEVC解码->H264编码 全转码,
  板子HEVC流坏NALU多 + ffmpeg CPU满载(92%) + "More than 1000 frames duplicated" -> ffmpeg内部积压越攒越多 -> 画面滞后到60秒
- 根治:
  1) 板子rkipc.ini 改 output_data_type=H.264 (主+子码流), 备份 rkipc.ini.h265bak
  2) 板子S99rkipc 加 LD_LIBRARY_PATH=/oem/usr/lib (否则/librockit.so加载失败)
  3) 本机ffmpeg转推改 -c:v copy (零转码):   板子H264 -> copy -> 云mediamtx
  4) 结果: ffmpeg CPU 92% -> ~0.1%, 无decode积压, 延迟从60s骤降到网络限
- 验证: 板子流=H.264 704x576, 云=H.264, ffmpeg copy正常运行
- 恢复H.265(若要): cp /userdata/rkipc.ini.h265bak /userdata/rkipc.ini; 重启rkipc
- 注意: 板子rkipc重启需 LD_LIBRARY_PATH=/oem/usr/lib (S99rkipc已修)
- 现象: 网络延迟数字小, 画面延迟数字大(缓冲多), 电脑手机都5-6秒
- 已排除: 关键帧密(0.48s GOP-12) ✓, mediamtx无大缓冲 ✓, ffmpeg转推zerolatency ✓
- 根因: 本机->腾讯云 ping 偶发大尖峰(最高2500ms)+丢包(10%), 间歇性网络抖动
  WebRTC浏览器为平滑播放把jitter buffer拉大补偿抖动/丢包 -> 画面持续滞后
- 二次30ping无尖峰(28/30<300ms) 证明抖动是间歇性/线路不稳
- 建议: 改善到腾讯云线路质量(选就近/低丢包线路), 或接受间歇抖动带来的缓冲
- 转推参数(低延迟): -fflags nobuffer -flags low_delay -preset ultrafast -tune zerolatency -g 12

## 延迟根治第二轮 (2026-09-11, 实测数据驱动) ✅
板子 192.168.3.67 实测:
- 板->云 ping: **ICMP 丢包 20~23%**, RTT 126~155ms (稳定无尖峰) — 局域网基线 0% 丢包/0.94ms
- 上行吞吐实测 ~10.5 Mbit/s; 主码流 max_rate 仅 2048kbps -> 带宽不是瓶颈, **丢包才是**
- 注: ICMP 丢包高可能含路由器 ICMP 限速, 但 RTT 126ms 稳定偏高是实打实的

### 改动 1: 主码流 GOP 25 -> 12, 码率收紧 (VERIFIED 文档此前只改了编码格式)
- 根因: 中继是 `-c:v copy` **零转码**, 所以命令行 `-g 12` **根本不生效**, GOP 完全由板子 rkipc 决定
- `gop = 25` @25fps = 1秒一个 I 帧 -> 20% 丢包下丢一个 P 帧要等**最多1秒**才恢复
- 已改 `/userdata/rkipc.ini`: `gop = 12` (0.48s), `max_rate` 2048->1024, `mid_rate` 1536->768
- 备份: `/userdata/rkipc.ini.bak_lat`

### 改动 2: 子码流 H.265 -> H.264 (此前文档声称已改, 实际没改)
- 实测 `/live/1` 一直是 `hevc (Main)` 704x576, 与 VERIFIED_webrtc.md:85 的描述不符
- 已修正, 现两条码流均为 H.264

### 改动 3: 明确局域网直连为第一优先 (真正的低延迟路径)
- 板载 mediamtx (`/root/mediamtx/mediamtx.yml`) 直连 `rtsp://127.0.0.1:554/live/0`
  -> **零跳、不经公网、无 20% 丢包**, 局域网 WebRTC 实测 ~100ms
- 云端 mediamtx 路径必然承受 126ms RTT + 20% 丢包 -> 浏览器 jitter buffer 被拉大 -> 画面滞后
- 网页 `car/index.html`: 局域网 WHEP 超时 2500->4000ms (首选给足握手时间), 公网回退保持 2500ms
- HUD 新增路径标识: 显示「局域网直连」或「公网中继」, 一眼看出当前走的哪条

### 结论: 局域网内请用 http://192.168.3.67:8889/car/ 或控制页(第一条 WHEP)
公网访问受限于 板->云 链路质量(20%丢包), 这是**线路问题, 非软件可解**。
若要改善公网延迟, 需换低丢包的就近线路(如国内中转/同省节点)。

## 图传"掉线"根因: 看门狗每5分钟自杀 (2026-09-11 已修复) ✅
### 症状
画面每隔几分钟断一次, 几秒后自己恢复。

### 根因: relay_loop.sh 的看门狗逻辑缺陷 (非网络问题)
原代码:
```sh
W=0
while kill -0 $FP 2>/dev/null && [ $W -lt 60 ]; do
  sleep 5
  W=$((W+1))
done
kill -9 $FP 2>/dev/null      # <== 无论 ffmpeg 健康与否, 到点就杀
```
`W` 计到 60 * 5s = **300 秒**, 即**每 5 分钟无条件 kill -9 一个完全健康的 ffmpeg**。
"看门狗"本意是防僵死, 实际写成了"定时重启", 于是每次重启都断流几秒。

### 日志实证 (间隔精确 5分02秒, 与推流状态无关)
```
18:04:32 relay reconnect
18:09:27 relay reconnect     (+4:55)
18:14:31 relay reconnect     (+5:04)
18:19:33 relay reconnect     (+5:02)
18:24:35 relay reconnect     (+5:02)
18:29:37 relay reconnect     (+5:02)
```
- ffmpeg 父进程确认为 `relay_loop.sh` (PPID 校验通过)
- 每次 `relay reconnect` 前均无错误信息 -> 不是异常退出, 是被 kill

### 修复
改为**只在真死 / 真僵死时**才重启, 去掉无条件定时重启:
- 进程退出 -> 立即重连 (正常路径)
- 进程仍在但 CPU 时间(`/proc/PID/stat` utime+stime)连续 60 秒不增长 -> 判定僵死才杀
- 注: 本板 `/proc/PID/io` **不可读**(实测), 故不能用 IO 字节数作判据; CPU jiffies 可用

### 验证 (关键)
修复后连续观察 6 分钟(跨过原 5 分钟必杀点):
```
[5 min] ffmpeg pid=1656  重连次数=0
[6 min] ffmpeg pid=1656  重连次数=0
```
同一 PID 存活 6 分钟无重启, 云端 ffprobe 确认 H.264 720p25 正常收流 ✅

### 备注
- 原文件已备份 `/userdata/car/relay_loop.sh.bak`
- 修改后需 `kill` 旧的 relay_loop 进程并重启, 否则旧逻辑仍在内存中运行
  (`sh /etc/init.d/S97relay stop; kill -9 <relay_loop_pid>; sh /etc/init.d/S97relay start`)

## 已验证(服务器/协议层)
- RTSP推流到云端car(H.264) ffprobe确认 ✓
- WHEP同源 8082/car/whep 返 201 + 完整H264 SDP answer ✓
- WHEP Location=/car/whep/<id>(相对) + Accept-Patch: trickle-ice-sdpfrag ✓
- nginx /car/whep 前缀代理含子路径(候选提交) ✓
- UDP 8189(ICE) 公网tcpdump证实可达 ✓
- 云端mediamtx: WebRTC sessions created(reader+measure 均创建) ✓
- 说明: headless(Chrome无头) 无法建立WebRTC媒体连接(官方reader页也deadline exceeded),
  必须真实手机/浏览器打开验证画面与延迟

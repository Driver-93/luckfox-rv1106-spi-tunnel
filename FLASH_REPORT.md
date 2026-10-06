# 刷机完成报告 — 2026-09-22

## ✅ 刷机成功

| 项目 | 结果 |
|------|------|
| 固件 | `fw_4g/update_4g.img` (73MB) |
| 内核 | `5.10.160 #1` (自编译, arm-rockchip830-gcc 8.3.0) |
| **4G 驱动** | ✅ **`usbnet_probe` 符号已在内核** (`CONFIG_USB_NET_CDCETHER=y` 等) |
| 板子 IP | **192.168.3.68** (原 .67, DHCP 变了) |

## ✅ 刷机后恢复状态

| 项目 | 状态 |
|------|------|
| 四轮电机配置 | ✅ FL=65/64/67, FR=54/55/58, BL=49/50/51, BR=59/41/48 |
| 电池标定 | ✅ adc_ratio=8.34 (11.24V 实测) |
| 图传 | ✅ H.264 720p25, gop=12, max_rate=1024 |
| 推流到云端 | ✅ 云端确认 `1 track (H264)` |
| GPS uart4 | ✅ `/dev/ttyS4` + `hwcfg` overlay 已应用 |
| Python 依赖 | ✅ paho + periphery |
| 四大服务 | ✅ rkipc / ffmpeg / mediamtx / car_controller |
| 到云端网络 | ✅ 丢包 0%, 延迟 246ms |

## ⚠️ 刷机过程中踩的坑 (已解决)

### 1. `/userdata` 只有 2.2MB — 大文件静默丢失
- `hw.dtbo`(287B) / `rkipc-300w.ini`(16KB) / `mediamtx`(38MB) 推过去都是 **0 字节**
- 原因: `/tmp` 是 90MB tmpfs, 批量推送时被写满, 导致后续文件截断
- 修复: **逐个文件单独推送**, 推完立即核对大小
- 教训: 大文件直接推到目标路径, 不要经 /tmp

### 2. SSH 免密失效 — `/root` 属主错误
```
sshd: Authentication refused: bad ownership or modes for directory /root
```
- `/root` 属主是 `1000:1001` 而非 `root`
- 修复: `chown root:root /root && chmod 700 /root`

### 3. 看门狗跑了两个实例
- `relay_loop.sh` 有 2 个进程, 各自拉起一个 ffmpeg, 互相打架
- 修复: `killall -9 relay_loop.sh` 后只启一个

### 4. 🔴 rkipc RTSP socket 泄漏 (87 个 CLOSE_WAIT)
- 现象: 554 端口存在大量 `CLOSE_WAIT`, `recv-q` 卡 81 字节, 新连接全部卡死
- **关键: 杀掉 rkipc 后这些 socket 依然存在** (内核态残留)
- 修复: **重启板子**才清掉
- 教训: 频繁拉起/杀掉 rkipc 客户端测试会积累泄漏; 重启后 `CLOSE_WAIT=0` 正常

## 📌 常用命令 (IP 已变)

```bash
# 板子新 IP: 192.168.3.68
ssh -i id_ed25519 root@192.168.3.68 "命令"

# 重启摄像头 (注意 LD_LIBRARY_PATH)
ssh -i id_ed25519 root@192.168.3.68 "kill -9 \$(pidof rkipc); sleep 3; LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib setsid /oem/usr/bin/rkipc -a /oem/usr/share/iqfiles >/tmp/rkipc.log 2>&1 &"

# ADB (备用, 但跑 ffmpeg 时容易掉线)
adb connect 192.168.3.68:5555
```

## 🔜 待验证: 4G 上网

**新固件的核心目的还没验证** —— 因为刷机前拔了模组的 USB 线。

**请把 4G 模组的 USB 线插回去**(板子 USB 口, 现在是 host 模式), 然后:

```bash
# 1. 模组是否枚举
ssh -i id_ed25519 root@192.168.3.68 "ls /sys/bus/usb/devices/ | grep 1-1"

# 2. 是否出现新网卡 (关键!)
ssh -i id_ed25519 root@192.168.3.68 "ls /sys/class/net/"

# 3. AT 查询 + SIM 解锁
ssh -i id_ed25519 root@192.168.3.68 "python3 /userdata/car/ec801e_at.py"
ssh -i id_ed25519 root@192.168.3.68 "python3 /userdata/car/ec801e_unlock.py"
```

**期望**: `/sys/class/net/` 里出现模组的网卡 (之前只有 eth0/lo/usb0, 模组网卡因为缺 `cdc_ether` 一直不存在)。出现即证明刷机目的达成。

## 备份与工具

- `board_full_backup_20260911_2138/` — 完整备份 (26 文件 68MB, 含 pylibs.tar)
- `restore_after_flash.sh` — 恢复脚本 (注意: 分批推送, 大文件单独核对)
- `fw_4g/update_4g.img` — 本次刷入的固件

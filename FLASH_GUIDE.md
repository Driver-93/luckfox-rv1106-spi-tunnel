# 刷机后恢复 — 操作手册

## 一、需要准备的文件 (都在本目录)

```
fw_4g/update_4g.img              ← 新固件 (73MB, 含 4G ECM 驱动)
_flash_4g.bat                    ← 刷机脚本
board_full_backup_20260911_2138/ ← 板子完整备份 (含 pylibs.tar)
restore_after_flash.sh           ← 恢复脚本
```

## 二、刷机步骤

### 1. 板子断电，拔掉 4G 模组的 USB 线
> 板子 USB 口现在是 **host 模式**给 4G 模组用；刷机需要它做 **device 模式**接电脑。

### 2. 按住 BOOT 键，插 USB 到电脑，松开 BOOT
电脑会识别到 **Rockusb 设备**。若没识别，装 `DriverAssitant_v5.1.1.zip` 里的驱动。

### 3. 双击 `_flash_4g.bat`
脚本会自动等待设备并刷入，约 1~3 分钟。

### 4. 刷完板子会自动重启

## 三、恢复配置

刷完板子 IP 会重新 DHCP（可能还是 `192.168.3.67`）。

```powershell
# 1. 传备份到板子
scp -i id_ed25519 -r board_full_backup_20260911_2138 root@192.168.3.67:/tmp/restore

# 2. 跑恢复脚本
ssh -i id_ed25519 root@192.168.3.67 "sh /tmp/restore/restore.sh"
```

恢复脚本会做这些事：
1. `/userdata/car/*` — 控制器、网页、脚本
2. `hwcfg/hw.dtbo` — uart4 + USB host 的 DTB overlay
3. `/etc/init.d/S96hwcfg, S97relay, S98mediamtx, S99car`
4. `rkipc-300w.ini` — **改出厂源文件**（否则重启被覆盖，GOP 会回到 25）
5. `/root/ffmpeg` + `/root/mediamtx/`
6. **Python 包 `paho-mqtt`** ← 新固件没有，必须恢复
7. `/etc/luckfox-car/car_config.json` 软链
8. 启动全部服务

## 四、刷完必须验证

```bash
# 1. 四轮接线配置还在吗 (物理接线不受刷机影响)
ssh root@192.168.3.67 "cat /userdata/car/car_config.json"

# 2. 4G 驱动是否生效 (本固件的核心目的)
ssh root@192.168.3.67 "ls /sys/bus/usb/devices/ | grep 1-1"     # 模组枚举
ssh root@192.168.3.67 "python3 /userdata/car/ec801e_at.py"      # AT 查询
ssh root@192.168.3.67 "ls /sys/class/net/"                      # 应出现 usb0/usb1 网卡

# 3. 图传
ssh root@192.168.3.67 "grep -E '^gop' /userdata/rkipc.ini"      # 应为 12
```

## 五、关键风险提醒

| 风险 | 说明 | 应对 |
|------|------|------|
| **paho-mqtt 缺失** | 新 rootfs 没这个包，控制器起不来 | 已备份 `pylibs.tar`，恢复脚本会装 |
| **DTB overlay 兼容性** | `hw.dtbo` 是针对旧 DTB 编译的 | 路径相同 (`serial@ff4e0000` / `usb@ffb00000`)，应可应用；若失败需重新编译 overlay |
| **GPS / 4G 依赖 USB host** | 靠 overlay 切换 | 失败则重新编 DTB 把 host 模式写死 |
| **4G 仍需内核支持** | 本固件已把 `CDC_ETHER` 等编为 `=y` | 刷完用 `ls /sys/class/net/` 验证 |
| **配置被清空** | `/userdata` 和 `/root` 都会重置 | 备份齐全 (25 文件 67MB) |

## 六、四轮接线（物理，不受刷机影响）

| 轮子 | 通道 | 物理脚 | GPIO |
|------|------|--------|------|
| 左前 | C | 24/25/27 | 65/64/67 |
| 右前 | A | 4/5/9 | 54/55/58 |
| 左后 | D | 14/15/16 | 49/50/51 |
| 右后 | B | 10/11/12 | 59/41/48 |

> 物理 17/19/20 已确认损坏，勿再接。

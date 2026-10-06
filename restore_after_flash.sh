#!/bin/sh
# ============================================================
#  刷机后一键恢复脚本 (在板子上运行)
#  刷固件会清空 /userdata 和 /oem 的改动, 本脚本把之前调好的全部恢复
#
#  前置: 先把备份目录传到板子
#    scp -r board_full_backup_xxx root@192.168.3.67:/tmp/restore/
#
#  然后: sh /tmp/restore/restore.sh
# ============================================================
set -e
SRC=$(dirname "$0")
echo "=== 恢复源: $SRC ==="
ls "$SRC" | head -30

echo
echo "########## 1. 恢复 /userdata/car (控制器+网页+脚本) ##########"
mkdir -p /userdata/car
if [ -d "$SRC/car" ]; then
  cp -f "$SRC/car/"* /userdata/car/ 2>/dev/null || true
  rm -rf /userdata/car/__pycache__
  echo "  已恢复 $(ls /userdata/car | wc -l) 个文件"
else
  echo "  ⚠ 未找到 $SRC/car"
fi

echo
echo "########## 2. 恢复 hwcfg (uart4 + usb host overlay) ##########"
mkdir -p /userdata/hwcfg
[ -f "$SRC/hwcfg/hw.dtbo" ] && cp -f "$SRC/hwcfg/hw.dtbo" /userdata/hwcfg/ && echo "  hw.dtbo OK"

echo
echo "########## 3. 恢复 init 脚本 ##########"
for s in S96hwcfg S97relay S98mediamtx S99car; do
  if [ -f "$SRC/$s" ]; then
    cp -f "$SRC/$s" /etc/init.d/$s
    chmod +x /etc/init.d/$s
    echo "  $s OK"
  fi
done

echo
echo "########## 4. 恢复 rkipc.ini (低延迟: gop=12) ##########"
# 关键: RkLunch.sh 每次开机用 /oem/usr/share/rkipc-300w.ini 覆盖 /userdata/rkipc.ini
#       所以必须改出厂源文件
if [ -f "$SRC/rkipc-300w.ini.oem" ]; then
  cp -f "$SRC/rkipc-300w.ini.oem" /oem/usr/share/rkipc-300w.ini
  cp -f "$SRC/rkipc-300w.ini.oem" /userdata/rkipc.ini
  echo "  已改出厂源文件 + 运行文件 (重启后不会被覆盖)"
  grep -E '^gop|^max_rate' /userdata/rkipc.ini | head -2
fi

echo
echo "########## 5. 恢复 /root 工具 (ffmpeg + mediamtx) ##########"
if [ -f "$SRC/ffmpeg_armhf" ]; then
  cp -f "$SRC/ffmpeg_armhf" /root/ffmpeg
  chmod +x /root/ffmpeg
  echo "  ffmpeg OK ($(ls -l /root/ffmpeg | awk '{print $5}') B)"
fi
if [ -d "$SRC/root_mediamtx" ]; then
  mkdir -p /root/mediamtx
  cp -f "$SRC/root_mediamtx/"* /root/mediamtx/ 2>/dev/null || true
  chmod +x /root/mediamtx/mediamtx 2>/dev/null || true
  echo "  mediamtx OK"
fi

echo
echo "########## 5.5 恢复 Python 依赖 (paho-mqtt 缺失!) ##########"
# 新固件 built-in 了 periphery, 但【没有 paho-mqtt】, 且无 pip
# 两边 Python 都是 3.11, 所以直接拷 site-packages 可用
SP=/usr/lib/python3.11/site-packages
mkdir -p "$SP"
if [ -f "$SRC/pylibs.tar" ]; then
  cd "$SP" && tar xf "$SRC/pylibs.tar" 2>/dev/null || true
  echo "  已解包 pylibs.tar 到 $SP"
fi
echo "--- 验证导入 ---"
python3 -c "import paho.mqtt.client as m; print('  paho OK', m.__file__)" 2>&1 | head -2
python3 -c "import periphery; print('  periphery OK', periphery.__version__)" 2>&1 | head -2

echo
echo "########## 6. 确认 /etc/luckfox-car 软链 ##########"
if [ ! -e /etc/luckfox-car/car_config.json ]; then
  mkdir -p /etc/luckfox-car
  ln -sf /userdata/car/car_config.json /etc/luckfox-car/car_config.json
  echo "  已建立软链"
else
  echo "  软链已存在"
  ls -l /etc/luckfox-car/car_config.json
fi

echo
echo "########## 7. 验证 4G 驱动 (本固件的核心目的) ##########"
echo "--- 内核 config (编译进内核应为 y) ---"
if [ -f /proc/config.gz ]; then
  zcat /proc/config.gz | grep -E 'CONFIG_USB_NET_CDCETHER|CONFIG_USB_USBNET'
else
  echo "  (无 /proc/config.gz, 用实际设备验证)"
fi
echo "--- 模组枚举 ---"
ls /sys/bus/usb/devices/ | grep -E '^1-1' && echo "  4G 模组在 ✓" || echo "  ⚠ 4G 模组未检测到"

echo
echo "########## 8. 启动服务 ##########"
sh /etc/init.d/S96hwcfg 2>/dev/null || true
sh /etc/init.d/S98mediamtx start 2>/dev/null || true
sh /etc/init.d/S97relay start 2>/dev/null || true
sh /etc/init.d/S99car start 2>/dev/null || true
sleep 8

echo
echo "=== 服务状态 ==="
ps | grep -E '[r]kipc|[f]fmpeg|[m]ediamtx|[c]ar_controller'
echo
echo "=== 电机配置 ==="
python3 -c "
import json
c=json.load(open('/userdata/car/car_config.json'))
for k in ('FL','FR','BL','BR'):
    p=c['pin'][k]
    print('  %-3s IN1=%-3d IN2=%-3d PWM=%-3d' % (k,p['IN1'],p['IN2'],p['PWM']))
" 2>/dev/null || echo "  (读取失败)"

echo
echo "########## 恢复完成 ##########"

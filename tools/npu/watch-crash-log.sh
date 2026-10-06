#!/bin/sh
# 崩溃日志采集器。放在板子上跑。
# 为什么需要它: 重启会清空内存里的 dmesg, 所以必须在**崩溃前**持续落盘,
# 而且必须写在 SD 卡上 (重启后仍在)。这是不接串口也能抓到崩溃现场的办法。
LOG=/mnt/sdcard/npu/crashwatch.log
BOOTLOG=/mnt/sdcard/npu/boots.log

echo "=== boot $(date '+%m-%d %H:%M:%S') uptime=$(cut -d. -f1 /proc/uptime)s ===" >> "$BOOTLOG"

while true; do
  {
    echo "--- $(date '+%H:%M:%S') uptime=$(cut -d. -f1 /proc/uptime)s load=$(cut -d' ' -f1 /proc/loadavg) ---"
    dmesg | tail -25
    echo "  mem: $(free | sed -n 2p)"
    echo "  procs: rkipc=$(ps | grep -c '[r]kipc') mediamtx=$(ps | grep -c '[m]ediamtx')"
  } >> "$LOG" 2>/dev/null

  # 限制日志大小, 保留最后 2000 行
  n=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  if [ "$n" -gt 4000 ]; then
    tail -2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
  fi
  sleep 1
done

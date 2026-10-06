#!/bin/sh
echo "=== 1) watchdog 设备 ==="
ls -l /dev/watchdog* 2>&1

echo ""
echo "=== 2) 内核 watchdog 驱动 ==="
ls /sys/class/watchdog/ 2>/dev/null
for w in /sys/class/watchdog/*; do
  [ -d "$w" ] || continue
  echo "--- $w ---"
  for f in identity state timeout timeout_min timeout_max pretimeout pretimeout_available_timeouts; do
    [ -f "$w/$f" ] && echo "  $f = $(cat $w/$f 2>/dev/null)"
  done
done

echo ""
echo "=== 3) watchdogd 是否在跑 ==="
ps | grep '[w]atchdog' || echo "  (没有 watchdogd)"

echo ""
echo "=== 4) 谁打开了 /dev/watchdog ==="
for p in /proc/[0-9]*; do
  pid=$(basename $p)
  for fd in $p/fd/*; do
    tgt=$(readlink $fd 2>/dev/null)
    case "$tgt" in
      *watchdog*) echo "  pid=$pid ($(cat $p/comm 2>/dev/null)) -> $tgt" ;;
    esac
  done
done 2>/dev/null | head

echo ""
echo "=== 5) dmesg 里的 watchdog ==="
dmesg | grep -i watchdog | head -10

echo ""
echo "=== 6) 内核模块 ==="
lsmod | grep -iE 'wdt|watchdog' || echo "  (无 wdt 模块, 可能是内建)"

echo ""
echo "=== 7) 设备树里的 wdt ==="
ls /proc/device-tree/ 2>/dev/null | grep -i wdt
cat /proc/device-tree/wdt*/status 2>/dev/null
echo "compatible: $(cat /proc/device-tree/wdt*/compatible 2>/dev/null | tr '\0' ' ')"

echo ""
echo "=== 8) 当前 load / 内存 ==="
uptime
free | head -2

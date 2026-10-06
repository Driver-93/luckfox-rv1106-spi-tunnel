#!/bin/sh
echo "======== 硬件看门狗验证 ========"
echo "uptime: $(cat /proc/uptime | awk '{print $1}') 秒"
echo ""
echo "--- 1) 内核版本 (确认是新烧的) ---"
cat /proc/version

echo ""
echo "--- 2) /dev/watchdog 是否存在 ---"
ls -l /dev/watchdog* 2>&1

echo ""
echo "--- 3) watchdog 设备类 ---"
ls /sys/class/watchdog/ 2>/dev/null || echo "  (空)"

echo ""
echo "--- 4) 看门狗属性 ---"
for w in /sys/class/watchdog/*; do
    [ -d "$w" ] || continue
    echo "  $w:"
    for f in identity state timeout timeout_min timeout_max pretimeout; do
        [ -f "$w/$f" ] && echo "    $f = $(cat $w/$f 2>/dev/null)"
    done
done

echo ""
echo "--- 5) 驱动是否加载 ---"
dmesg | grep -iE 'watchdog|wdt' | head -10 || echo "  (dmesg 无相关输出)"
lsmod | grep -i wdt || echo "  (无 wdt 模块, 应为内建)"

echo ""
echo "--- 6) S21wdt 服务状态 ---"
/etc/init.d/S21wdt status 2>&1

echo ""
echo "--- 7) 喂狗日志 ---"
cat /tmp/hw_wdt.log 2>/dev/null || echo "  (无日志)"

echo ""
echo "--- 8) 其它服务是否照常 ---"
echo "  lo:      $(ip addr show lo | grep -c '127.0.0.1')"
echo "  spitun0: $(ip addr show spitun0 | grep -c '10.77.0.2')"
ps | grep -E '[w]eb_server|[m]ediamtx|[r]kipc|[S]24spinet' | sed 's/^/  /'
echo "  端口: $(netstat -tln | grep -cE ':80 |:554 |:8889|:8189|:8888') / 5"
echo "  C5: $(cat /sys/class/net/spitun0/c3_status 2>/dev/null | head -c 80)"

echo ""
echo "--- 9) 隧道 bad_magic ---"
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]*' | tail -1

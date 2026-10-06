#!/bin/sh
echo "uptime: $(cat /proc/uptime | cut -d' ' -f1) 秒"
echo ""
echo "--- 全部服务状态 ---"
for s in S20lo S21wdt S22spinet S23web S24spinet_wd S25mediamtx; do
    if [ -x "/etc/init.d/$s" ]; then
        out=$(/etc/init.d/$s status 2>&1 | head -1)
        printf '  %-14s %s\n' "$s" "$out"
    else
        printf '  %-14s 缺失!\n' "$s"
    fi
done

echo ""
echo "--- 进程 ---"
ps | grep -E '[w]atchdog|[w]eb_server|[m]ediamtx|[r]kipc|[S]24spinet' | sed 's/^/  /'

echo ""
echo "--- 监听端口 ---"
netstat -tln | grep -E ':80 |:554 |:8889|:8189|:8888|:8554' | sed 's/^/  /'

echo ""
echo "--- 隧道状态 ---"
cat /sys/class/net/spitun0/c3_status

echo ""
echo "--- SPI 诊断 ---"
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]* bad_csum=[0-9]*' | tail -1 | sed 's/^/  /'

echo ""
echo "--- 硬件看门狗 ---"
ls -l /dev/watchdog | sed 's/^/  /'
/etc/init.d/S21wdt status | sed 's/^/  /'

echo ""
echo "--- 磁盘 / 内存 ---"
df -h /userdata | tail -1 | sed 's/^/  /'
free | head -2 | sed 's/^/  /'

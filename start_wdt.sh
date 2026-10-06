#!/bin/sh
echo "--- 启动 ---"
/etc/init.d/S21wdt start

echo ""
echo "--- 状态 ---"
/etc/init.d/S21wdt status

echo ""
echo "--- watchdog 进程 ---"
ps | grep '[w]atchdog' || echo "  (没有)"

echo ""
echo "--- 喂狗日志 ---"
tail -5 /tmp/hw_wdt.log 2>/dev/null || echo "  (无日志)"

echo ""
echo "--- sysfs 目录内容 ---"
ls /sys/class/watchdog/watchdog0/

echo ""
echo "--- 属性值 ---"
for f in identity state timeout timeout_min timeout_max pretimeout bootstatus; do
    p=/sys/class/watchdog/watchdog0/$f
    if [ -f "$p" ]; then
        echo "  $f = $(cat $p 2>/dev/null)"
    fi
done

echo ""
echo "--- 谁打开了 /dev/watchdog ---"
for p in /proc/[0-9]*; do
    pid=$(basename $p)
    comm=$(cat $p/comm 2>/dev/null)
    for fd in $p/fd/*; do
        tgt=$(readlink $fd 2>/dev/null)
        case "$tgt" in
            *watchdog*) echo "  pid=$pid ($comm) -> $tgt" ;;
        esac
    done
done 2>/dev/null | head

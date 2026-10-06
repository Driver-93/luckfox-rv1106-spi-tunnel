#!/bin/sh
# 决定性验证: 打开 /dev/watchdog 但**不喂狗**, 硬件应在 30 秒内复位板子。
#
# 这模拟的正是"内核硬卡死"的状态 —— 喂狗进程得不到调度, 无法写设备。
# 如果硬件看门狗工作正常, 板子会在 ~30 秒后自己重启。
#
# 注意: 本脚本会**主动把板子弄重启**, 这是预期行为。
OUT=/tmp/wdt_test.log
: > $OUT
exec >> $OUT 2>&1

echo "=== $(date '+%H:%M:%S') 看门狗复位测试开始 ==="

echo "--- 1) 先停掉喂狗服务 (它会释放 /dev/watchdog) ---"
/etc/init.d/S21wdt stop
sleep 2

echo "--- 2) 确认设备已无人持有 ---"
holder=""
for q in /proc/[0-9]*; do
    for fd in $q/fd/*; do
        case "$(readlink $fd 2>/dev/null)" in
            /dev/watchdog) holder="$q" ;;
        esac
    done
done
echo "  持有者: ${holder:-无}"

echo "--- 3) 记录当前 uptime (复位后它会变小) ---"
cat /proc/uptime | awk '{print "  uptime =", $1, "秒"}'

echo "--- 4) 打开 /dev/watchdog 但从不写入 ---"
echo "  预期: 约 30 秒后硬件自动复位"
echo "  如果 90 秒后板子还没重启, 说明看门狗没真正生效"
sync

# 用 fd 3 打开设备, 然后一直睡 —— 不写任何数据。
# 30 秒超时一到, 硬件直接复位 SoC, 这个 sleep 会被强行打断。
exec 3>/dev/watchdog
sleep 300

# 能走到这里说明**没有复位** —— 测试失败
echo "!!! $(date '+%H:%M:%S') 已经过了 300 秒还没复位, 看门狗未生效 !!!"

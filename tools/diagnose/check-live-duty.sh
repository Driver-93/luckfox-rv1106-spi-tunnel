#!/bin/sh
# Confirm the FULL path (HTTP -> handler -> motor -> sysfs) actually drives the
# wheels, without letting the 0.5s deadman confuse the reading.
#
# A single POST is not enough: failsafe_s is 0.5s, so by the time wget returns
# and we cat the sysfs file, the deadman has already stopped the car and duty is
# back to 0 -- which looks exactly like "the command did nothing". That misled me
# once already. So: temporarily widen the deadman, send one low-speed command,
# read the duty while it is definitely still live, then stop and restore.
#
# Speed is kept at 10% so the car barely creeps during the ~1s window.
set -e
API=http://127.0.0.1/api/cmd
JSON='Content-Type: application/json'

post() {
    wget -q -O - --post-data="$1" --header="$JSON" "$2" 2>/dev/null || true
}

echo "--- 把失控超时临时放宽到 2s (只为这次测量) ---"
post '{"t":2}' http://127.0.0.1/api/failsafe
echo

echo "--- 发 s=10 前进, 立刻读 sysfs ---"
post '{"c":"forward","s":10}' "$API"
for c in 10 11 6 8; do
    printf '  chip%-3s duty=%-8s enable=%s polarity=%s\n' "$c" \
        "$(cat /sys/class/pwm/pwmchip$c/pwm0/duty_cycle 2>/dev/null)" \
        "$(cat /sys/class/pwm/pwmchip$c/pwm0/enable 2>/dev/null)" \
        "$(cat /sys/class/pwm/pwmchip$c/pwm0/polarity 2>/dev/null)"
done
post '{"c":"stop"}' "$API"
echo "  (已 stop)"

echo "--- s=60 对比 ---"
post '{"c":"forward","s":60}' "$API"
printf '  chip10 duty=%s\n' "$(cat /sys/class/pwm/pwmchip10/pwm0/duty_cycle)"
post '{"c":"stop"}' "$API"
echo "  (已 stop)"

echo
echo "--- 恢复失控超时到 0.5s, 并确认已经停车 ---"
post '{"t":0.5}' http://127.0.0.1/api/failsafe
echo
post '{"c":"stop"}' "$API"
echo
echo "--- 停车后四路 ---"
for c in 10 11 6 8; do
    printf '  chip%-3s duty=%-8s enable=%s\n' "$c" \
        "$(cat /sys/class/pwm/pwmchip$c/pwm0/duty_cycle)" \
        "$(cat /sys/class/pwm/pwmchip$c/pwm0/enable)"
done
echo
echo "期望: s=10 -> duty=100000, s=60 -> duty=600000 (period=1000000)"

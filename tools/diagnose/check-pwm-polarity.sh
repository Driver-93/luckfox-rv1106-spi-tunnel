#!/bin/sh
# Assert the motor PWM polarity is NORMAL on all four channels.
#
# Why this exists: this board's Rockchip PWM defaults to polarity=inversed, which
# makes duty_cycle mean LOW time. Since the whole speed model is "duty = 速度值%",
# an inverted channel mirrors the entire speed axis -- value 10 drives at 90%,
# value 100 stops the motor. Reported by the user as "滑条往右拖数字变大反而
# 更慢". The software is completely correct in that state, so nothing in the logs
# looks wrong: this attribute is the only place the fault is visible.
#
# car_motor.HwPwm.export() now sets polarity=normal before enabling. Run this
# after any change to the PWM setup, or whenever the speed slider feels reversed.
#
# Usage (on the board):  sh tools/diagnose/check-pwm-polarity.sh
CONF=/userdata/car/car_config.json
EXPECT_CHIPS=$(python3 -c "
import json
c=json.load(open('$CONF'))
print(' '.join('%s:%s' % (v,k) for k,v in sorted(c['pwm_chip'].items())))
" 2>/dev/null)

echo "expect FL:10 FR:8 BL:6 BR:11 per config; found:"
echo "  $EXPECT_CHIPS"
echo

bad=0
for c in /sys/class/pwm/pwmchip*; do
    [ -d "$c" ] || continue
    for p in "$c"/pwm*; do
        [ -d "$p" ] || continue
        pol=$(cat "$p/polarity" 2>/dev/null)
        per=$(cat "$p/period" 2>/dev/null)
        if [ "$pol" = "normal" ]; then
            echo "  OK   $p polarity=normal period=$per"
        else
            echo "  FAIL $p polarity=$pol  <-- 速度轴会反! (应该 normal)"
            bad=$((bad+1))
        fi
    done
done

echo
if [ "$bad" = "0" ]; then
    echo "结论: 极性全部 normal -> duty 越大越快, 速度滑条方向正确。"
    exit 0
fi
echo "结论: $bad 路极性不对。检查 car_motor.HwPwm.export() 里的 polarity 写入"
echo "      (必须在 enable 之前; 若通道已被使能, 先 enable=0 再改)。"
exit 1

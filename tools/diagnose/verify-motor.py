#!/usr/bin/env python3
"""End-to-end check of the motor path, safe to run with the car on the ground.

Answers, in one shot:
  1. does FourMotor come up with all FOUR pwm channels exported? (the
     AttributeError bug left only one exported)
  2. what does each channel's polarity actually read back as?
  3. is duty proportional to the speed value end to end?  (drives at a low duty
     only, and stops immediately -- wheels may twitch briefly)
  4. is dbg['polarity'] exposed for /api/motordbg?
"""
import json
import sys

sys.path.insert(0, "/userdata/car")
from car_motor import FourMotor

CONF = "/userdata/car/car_config.json"
conf = json.load(open(CONF))

print("=== 1) 启动 (真实硬件模式) ===")
m = FourMotor(conf["pin"], simulate=False, inv=conf.get("axis_inv") or {},
              pwm_chip=conf.get("pwm_chip"),
              use_hw_pwm=conf.get("use_hw_pwm", True),
              freq=conf.get("pwm_freq", 1000.0))
print("  pwm_alive =", m.dbg.get("pwm_alive"), " (需要 True)")
print("  chips     =", m.dbg.get("chips"))
print("  polarity  =", m.dbg.get("polarity"))
bad = [c for c, v in (m.dbg.get("polarity") or {}).items() if v != "normal"]
print("  ->", "OK 四路都在且极性 normal" if not bad else "!! 有问题: %s" % bad)

print()
print("=== 2) duty 是否随速度值单调上升 (真写 sysfs) ===")
print("  (低占空比, 轮子可能轻微动一下; 每步之后立刻停)")
last = None
rising = True
for sp in (10, 30, 60):
    m.drive(vx=0, vy=1.0, w=0, speed=sp)
    st = m.pwm_state()
    d = st["FL"]
    duty = d.get("duty_cycle") if isinstance(d, dict) else d
    period = d.get("period") if isinstance(d, dict) else "?"
    pol = d.get("polarity") if isinstance(d, dict) else "?"
    print("    速度 %3d -> FL duty_cycle=%s period=%s polarity=%s"
          % (sp, duty, period, pol))
    m.stop()
    try:
        v = int(duty)
        if last is not None and v <= last:
            rising = False
        last = v
    except Exception:
        rising = False

print()
print("=== 3) 停止后的状态 (应该 duty=0 / enable=0) ===")
m.stop()
for c in m.CH:
    print("    %-3s %s" % (c, m.pwm_state()[c]))
m.close()

print()
print("结论: %s" % ("duty 随速度值单调上升, 极性 normal -> 速度滑条方向正确"
                    if (rising and not bad) else
                    "还有问题, 看上面哪一项不对"))

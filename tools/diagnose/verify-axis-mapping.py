#!/usr/bin/env python3
"""Verify the mecanum direction mapping WITHOUT moving the car.

Uses FourMotor(simulate=True), which returns before touching any GPIO or PWM, so
nothing spins. It records the direction bits that drive() WOULD write, and
compares them against the conventions documented in tools/diagnose/check-axis.py:

    前进   vy=+1 -> FL+ FR+ BL+ BR+
    后退   vy=-1 -> FL- FR- BL- BR-
    右移   vx=+1 -> FL+ FR- BL- BR+
    左移   vx=-1 -> FL- FR+ BL+ BR-
    右转   w=+1  -> FL+ FR- BL+ BR-      (顺时针, 从上往下看)
    左转   w=-1  -> FL- FR+ BL- BR+

The bug under investigation: the page/joystick/keyboard all send a RIGHTWARD drag
as vx>0 (index.html keyApply: d/arrowright -> x += 1), and car_motor.drive()
documents "vx 横移(+右/-左)". So with a correct config, 右移 must come out
FL+ FR- BL- BR+. If axis_inv has vx=true, it comes out mirrored -- which is
exactly the reported "左右是反的".
"""
import json
import sys

sys.path.insert(0, "/userdata/car")
from car_motor import FourMotor

CONF = "/userdata/car/car_config.json"

EXPECT = {
    # name: (vx, vy, w, expected signs FL,FR,BL,BR)
    "前进": (0, 1, 0, "+", "+", "+", "+"),
    "后退": (0, -1, 0, "-", "-", "-", "-"),
    "右移": (1, 0, 0, "+", "-", "-", "+"),
    "左移": (-1, 0, 0, "-", "+", "+", "-"),
    "右转": (0, 0, 1, "+", "-", "+", "-"),
    "左转": (0, 0, -1, "-", "+", "-", "+"),
}


def probe(inv):
    """Return {action: 'FL FR BL BR' signs} with hardware untouched."""
    conf = json.load(open(CONF))
    m = FourMotor(conf["pin"], simulate=True, inv=inv,
                  pwm_chip=conf.get("pwm_chip"),
                  use_hw_pwm=conf.get("use_hw_pwm", True))
    out = {}
    for name, (vx, vy, w, *_want) in EXPECT.items():
        cap = {}
        m._set_dir = lambda d, _c=cap: _c.update(d)   # capture, do not print
        m.drive(vx=vx, vy=vy, w=w, speed=30)
        signs = []
        for c in ("FL", "FR", "BL", "BR"):
            in1, in2 = cap[c]
            signs.append("+" if in1 == 1 else ("-" if in2 == 1 else "0"))
        out[name] = signs
    return out


def report(title, inv):
    got = probe(inv)
    print("--- %s   axis_inv=%s ---" % (title, json.dumps(inv)))
    bad = []
    for name, (_vx, _vy, _w, a, b, c, d) in EXPECT.items():
        want = [a, b, c, d]
        g = got[name]
        ok = (g == want)
        if not ok:
            bad.append(name)
        print("   %s %-4s FL FR BL BR = %s   (want %s)"
              % ("OK  " if ok else "FAIL", name, " ".join(g), " ".join(want)))
    print("   -> %s" % ("all actions match the documented convention"
                        if not bad else "WRONG: " + ", ".join(bad)))
    return bad


print("=" * 74)
print("麦轮方向自检 (simulate 模式, 不碰硬件, 轮子不会转)")
print("=" * 74)
bad_now = report("当前配置", json.load(open(CONF)).get("axis_inv") or {})
print()
bad_fix = report("去掉 vx 取反之后", {})
print()
if bad_now and not bad_fix:
    print("结论: 当前 axis_inv 里的 vx 取反就是'左右是反的'的原因;")
    print("      去掉它之后六个动作全部符合文档约定。")
elif not bad_now:
    print("结论: 当前配置已经符合文档约定 -> 左右反了的原因不在这里。")
else:
    print("结论: 情况更复杂, 需要逐项看上面哪几个 FAIL。")

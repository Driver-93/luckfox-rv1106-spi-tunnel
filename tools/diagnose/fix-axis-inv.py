#!/usr/bin/env python3
"""Drop the erroneous vx inversion from car_config.json.

Why: the joystick, the A/D keys and the arrow keys all send a RIGHTWARD drag as
vx>0 (index.html keyApply: d/arrowright -> x += 1), and car_motor.drive()
documents "vx 横移(+右/-左)". With axis_inv.vx=true the whole left/right axis
comes out mirrored, which is the reported "控制键左右是反的". Verified with
_verify_axis.py in simulate mode (hardware untouched): with vx inverted, 右移
produces FL- FR+ BL+ BR- (i.e. it drives LEFT).

Backs up the original first, then validates the JSON it writes.
"""
import json
import os
import shutil
import time

CONF = "/userdata/car/car_config.json"
BAK = CONF + ".bak.axis"

shutil.copy2(CONF, BAK)
print("backed up: %s" % BAK)

with open(CONF, encoding="utf-8") as f:
    conf = json.load(f)

print("before: axis_inv = %s" % json.dumps(conf.get("axis_inv")))
conf["axis_inv"] = {}
print("after : axis_inv = %s" % json.dumps(conf.get("axis_inv")))

tmp = CONF + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(conf, f, ensure_ascii=False, indent=2)
    f.write("\n")

# 写回前先自己解析一遍: 这个文件是活配置, 写坏了 web_server 起不来。
with open(tmp, encoding="utf-8") as f:
    back = json.load(f)
assert back.get("axis_inv") == {}, "round-trip failed"
assert back.get("pin") == conf.get("pin"), "pin block changed unexpectedly"
os.replace(tmp, CONF)
print("written and re-parsed OK at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

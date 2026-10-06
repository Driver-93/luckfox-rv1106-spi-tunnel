#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轴向自检: 一次扫出"到底哪个方向反了", 不用靠猜。

用法 (板子上, 车要**架起来**让轮子空转!):
    python3 tools/diagnose/check-axis.py            # 只显示每个动作的四轮输出
    python3 tools/diagnose/check-axis.py --test     # 真转轮子, 你看着报结果

--test 会依次做: 前进 / 后退 / 右移 / 左移 / 右转 / 左转,
每次 1.5 秒、间隔 1 秒, 你在旁边看轮子, 最后它告诉你该改哪个开关。

⚠️ 安全: 一定要把车架起来 (轮子离地)。这个脚本会让轮子真的转。
"""
import json
import sys
import time

CONF = "/userdata/car/car_config.json"

# 麦轮运动学 (与 car_motor.drive 一致)
def wheels(vx, vy, w):
    fl = vy + vx + w
    fr = vy - vx - w
    bl = vy - vx + w
    br = vy + vx - w
    m = max(abs(fl), abs(fr), abs(bl), abs(br))
    if m > 1.0:
        fl, fr, bl, br = fl / m, fr / m, bl / m, br / m
    return {"FL": fl, "FR": fr, "BL": bl, "BR": br}


ACTIONS = [
    ("前进",  0,  1,  0, "车应该往**前**走"),
    ("后退",  0, -1,  0, "车应该往**后**走"),
    ("右移",  1,  0,  0, "车应该往**右**平移"),
    ("左移", -1,  0,  0, "车应该往**左**平移"),
    ("右转",  0,  0,  1, "车应该**顺时针**原地转 (从上看)"),
    ("左转",  0,  0, -1, "车应该**逆时针**原地转 (从上看)"),
]


def show_table():
    print("麦轮各动作的四轮输出 (+ = 正转, - = 反转)")
    print("=" * 78)
    print("%-8s %-10s %8s %8s %8s %8s" % ("动作", "(vx,vy,w)", "FL", "FR", "BL", "BR"))
    print("-" * 78)
    for name, vx, vy, w, _ in ACTIONS:
        d = wheels(vx, vy, w)
        print("%-8s (%+d,%+d,%+d) %8.2f %8.2f %8.2f %8.2f"
              % (name, vx, vy, w, d["FL"], d["FR"], d["BL"], d["BR"]))
    print()
    print("看这张表能推出该改哪个开关:")
    print("  * 前进 = 四轮全 +   -> 如果车往后走 => vy 要取反")
    print("  * 右移 = FL+ FR- BL- BR+   (对角同向)")
    print("    如果右移时车往左走        => vx 要取反")
    print("  * 右转 = FL+ FR- BL+ BR-   (左正右反)")
    print("    如果右转时车往左转        => w 要取反")
    print("  * 如果**前后也反了**       => vy 也要取反 (可能整体接反)")


def run_test():
    try:
        import periphery  # noqa: F401
    except Exception:
        print("!! 没有 periphery 模块, 不能在板上跑 --test")
        return
    sys.path.insert(0, "/userdata/car")
    try:
        conf = json.load(open(CONF))
    except Exception as e:
        print("!! 读 %s 失败: %s" % (CONF, e))
        return
    from car_motor import FourMotor
    m = FourMotor(conf["pin"], simulate=False, inv=conf.get("axis_inv") or {})
    m.start_pwm()

    print()
    print("=" * 78)
    print("开始实测。**请把车架起来, 轮子离地!**")
    print("每个动作 1.5 秒, 间隔 1 秒。看清楚车实际往哪走。")
    print("=" * 78)
    time.sleep(3)

    results = {}
    for name, vx, vy, w, expect in ACTIONS:
        print()
        print(">>> 接下来做: %s   ( %s )" % (name, expect))
        for i in range(3, 0, -1):
            print("    %d..." % i)
            time.sleep(1)
        m.drive(vx=vx, vy=vy, w=w, speed=45)
        time.sleep(1.5)
        m.stop()
        time.sleep(1.0)
        # 让用户报结果
        while True:
            a = input("    实际是哪个? [1]对  [2]反  [3]没动/看不出来 : ").strip()
            if a in ("1", "2", "3"):
                results[name] = a
                break
            print("    请输入 1 / 2 / 3")

    m.stop()
    print()
    print("=" * 78)
    print("结果")
    print("=" * 78)
    for name, vx, vy, w, _ in ACTIONS:
        r = results.get(name, "3")
        print("  %-6s %s" % (name, {"1": "正确 ✓", "2": "反向 ✗", "3": "没动/看不出"}[r]))

    print()
    print("=" * 78)
    print("建议改这里 (%s 的 axis_inv):" % CONF)
    print("=" * 78)
    inv = {}
    # 前后反 -> vy
    if results.get("前进") == "2" or results.get("后退") == "2":
        inv["vy"] = True
    # 横移反 -> vx
    if results.get("右移") == "2" or results.get("左移") == "2":
        inv["vx"] = True
    # 自转反 -> w
    if results.get("右转") == "2" or results.get("左转") == "2":
        inv["w"] = True

    if not inv:
        print('  "axis_inv": {}          <- 全部正确, 不用改')
    else:
        print('  "axis_inv": %s' % json.dumps(inv))
    print()
    print("改完重启 web_server 生效:")
    print("  /etc/init.d/S23web restart")
    print()
    print("注意: 这些判断基于你**肉眼**看到的结果。如果某个动作没动, 那可能不是")
    print("      方向问题而是接线/驱动问题, 单独查。")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    else:
        show_table()
        print()
        print("想实测? 把车**架起来**(轮子离地) 然后:")
        print("  python3 %s --test" % sys.argv[0])

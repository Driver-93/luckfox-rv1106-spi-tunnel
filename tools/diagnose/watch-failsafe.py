#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板子侧失控保护/链路观测器 (单进程版)。

⚠️ 2026-10-05 踩过的坑 (重要):
    上一版是 `sh` 循环里每 0.5 秒起一个 `python3 - <<PY` —— **在单核 A7 上
    每次 Python 启动就要几百毫秒**, 于是这个"观测器"自己把 CPU 打到 0% idle,
    web_server 线程和 250Hz 软件 PWM 线程都被饿死, 用户看到的正是
    "按了没反应"。诊断工具本身变成了故障源。

    现在改成**一个常驻 python 进程**, 采样只做一次 urllib 请求, 开销 <1%。

用法:
    /userdata/fs_watch.py [秒数] [采样间隔秒, 默认 0.5]
输出: /userdata/fs_watch.log
    epoch failsafe cmd_age dir vx c3_up_ms tx_pkts rx_pkts
"""
import json
import sys
import time
import urllib.request

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
IV = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
OUT = "/userdata/fs_watch.log"


def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return "-1"


def main():
    t0 = time.time()
    prev_up = None
    trips = 0
    with open(OUT, "w") as fo:
        fo.write("# epoch failsafe cmd_age dir vx c3_up_ms tx_pkts rx_pkts note\n")
        while time.time() - t0 < DUR:
            note = ""
            try:
                d = json.load(urllib.request.urlopen(
                    "http://127.0.0.1/api/status", timeout=3))
                up = d.get("tel", {}).get("c3", {}).get("up_ms")
                up = int(up) if up is not None else -1
                if prev_up is not None and up >= 0 and up < prev_up:
                    note = "C3-RESTART"
                prev_up = up
                fs = int(d.get("failsafe", -1))
                if fs > trips and trips >= 0:
                    note = (note + " FAILSAFE").strip()
                trips = max(trips, fs)
                fo.write("%.2f %d %.2f %s %.3f %d %s %s %s\n" % (
                    time.time(), fs, d.get("cmd_age", -1), d.get("dir", "?"),
                    d.get("vx", 0.0), up, rd("/sys/class/net/spitun0/statistics/tx_packets"),
                    rd("/sys/class/net/spitun0/statistics/rx_packets"), note))
            except Exception as e:
                fo.write("%.2f ERR -1 - - - - - - %s\n" % (time.time(), e))
            fo.flush()
            time.sleep(IV)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPI 帧失败率 / C3 重启 联合观测 (单进程, 开销极低)。

判定思路:
  * fail 计数 (bad_magic) 是**整帧丢失** —— 隧道没有重传, 这些帧里的
    IP 报文直接没了, 只能靠 TCP 重传, 表现就是丢包/卡顿。
  * 如果 fail 暴增和 C3 重启 (up_ms 归零) 同步发生, 那丢包就是 C3 复位造成的;
    如果 C3 一直很稳 (up_ms 单调涨) 而 fail 还在涨, 那就是 SPI 时序/模块的问题。
"""
import re
import subprocess
import sys
import time
import urllib.request
import json

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
IV = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

RE_F = re.compile(r"frames=(\d+) ok=(\d+) fail=(\d+) bad_magic=(\d+) bad_csum=(\d+)")
RE_IP = re.compile(r"ip_tx=(\d+) rx=(\d+) drop=(\d+)")


def dmesg_tail():
    try:
        return subprocess.run(["dmesg"], stdout=subprocess.PIPE,
                              timeout=5).stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def main():
    t0 = time.time()
    prev = None
    print("t(s)  frames_d  fail_d  fail%%   up_ms    rssi  note")
    while time.time() - t0 < DUR:
        d = dmesg_tail()
        fm = list(RE_F.finditer(d))
        im = list(RE_IP.finditer(d))
        if not fm:
            print("  (dmesg 里没有 spitun 计数行, 等下一轮)")
            time.sleep(IV)
            continue
        f = fm[-1]
        frames, ok, fail = int(f.group(1)), int(f.group(2)), int(f.group(3))
        ipdrop = int(im[-1].group(3)) if im else -1
        up = -1
        rssi = "?"
        try:
            s = json.load(urllib.request.urlopen(
                "http://127.0.0.1/api/status", timeout=4))
            c3 = s["tel"]["c3"]
            up = c3.get("up_ms", -1)
            rssi = c3.get("rssi", "?")
        except Exception:
            pass
        note = ""
        if prev is not None:
            df = frames - prev[0]
            dfail = fail - prev[1]
            pct = (100.0 * dfail / df) if df > 0 else 0.0
            if prev[2] is not None and up >= 0 and up < prev[2]:
                note = "C3-RESTART"
            if pct > 5:
                note = (note + " HIGH-LOSS").strip()
            print("%5.0f  %8d  %6d  %5.1f%%  %7d  %5s  %s" % (
                time.time() - t0, df, dfail, pct, up, rssi, note))
        prev = (frames, fail, up if up >= 0 else None)
        time.sleep(IV)


if __name__ == "__main__":
    main()

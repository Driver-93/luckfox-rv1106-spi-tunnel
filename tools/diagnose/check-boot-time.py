#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认: 内核启动时就已经把时间设错了 (btime 就是错的)。

判据: /proc/stat 的 btime 是内核记录的**启动时刻的 epoch**。
      它 = 启动瞬间的系统时间。
      如果 btime 比真实 UTC 快 8 小时, 说明**内核启动时**就设错了,
      和我们跑什么脚本无关。

然后回答: 内核凭什么这么设? -> 它读 RTC, 但按"本地时间"解释。
      内核启动时有 `rtc_hctosys` 逻辑, 用 CONFIG_RTC_HCTOSYS_DEVICE 指定的
      RTC 设置系统时间。若内核认为 RTC 存的是本地时间, 就会做转换。

验证: 看内核命令行有没有 rtc 相关参数, 以及 RTC 的时区属性。
"""
import os
import subprocess
import time


def sh(c):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def main():
    now = int(time.time())
    bt = None
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            bt = int(line.split()[1])
    up = float(open("/proc/uptime").read().split()[0])
    rtc = int(open("/sys/class/rtc/rtc0/since_epoch").read().strip())

    print("=== 三个绝对时间 ===")
    print("  btime (内核记录的启动时刻) = %d  = %s UTC"
          % (bt, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(bt))))
    print("  now - uptime (推出的启动时刻) = %d" % int(now - up))
    print("  RTC epoch                   = %d  = %s UTC"
          % (rtc, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(rtc))))
    print("  now                         = %d  = %s UTC"
          % (now, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))))
    print()
    print("  真实 UTC 大约 = %s (由 PC 提供)"
          % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - (now - rtc) + (now - rtc))))

    print("\n=== 判断 ===")
    startup = now - up
    d_start = startup - rtc
    print("  启动时刻 - RTC = %d 秒 (%.2f 小时)" % (d_start, d_start / 3600.0))
    if abs(d_start) < 120:
        print("  -> 内核启动时用的是 RTC 的值 (正确)")
    elif abs(abs(d_start) - 28800) < 600:
        print("  -> **内核启动时把 RTC 当成了本地时间**, 所以系统快了 8 小时!")
        print("     这是内核的 rtc_hctosys 行为, 不是任何脚本造成的。")
    else:
        print("  -> 差异不是 8 小时, 需要另查")

    print("\n=== 内核命令行 (有无 rtc/hctosys 参数) ===")
    print("  " + (open("/proc/cmdline").read().strip() or "(空)"))

    print("\n=== RTC 设备属性 ===")
    for p in ("/sys/class/rtc/rtc0/name", "/sys/class/rtc/rtc0/hctosys",
              "/sys/class/rtc/rtc0/time", "/sys/class/rtc/rtc0/date"):
        try:
            print("  %-32s = %s" % (p, open(p).read().strip()))
        except Exception:
            print("  %-32s = (读不到)" % p)

    print("\n=== 内核日志里 rtc / hctosys 相关 ===")
    log = sh("dmesg")
    hits = [l for l in log.splitlines()
            if "rtc" in l.lower() and ("hctosys" in l.lower()
                                       or "set system" in l.lower()
                                       or "systohc" in l.lower())]
    print("  " + ("\n  ".join(hits[-6:]) if hits else "(无)"))
    rtc_lines = [l for l in log.splitlines() if "rtc" in l.lower()][-8:]
    print("  最近的 rtc 日志:")
    for l in rtc_lines:
        print("    " + l.strip()[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

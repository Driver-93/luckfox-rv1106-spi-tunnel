#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板子侧抓帧 + 亮度统计 —— 用 v4l2-ctl 抓 raw, 自己算统计。

为什么不让 v4l2-ctl 直接出 JPEG: 它只能抓 ISP 输出的 raw (BG10/Bayer),
板子上没有能解 Bayer 的库。但做**亮度统计**不需要解马赛克 —— Bayer 的
每个像素值本身就是亮度采样, 直接统计就够用 (过曝比例、平均亮度、
暗部比例 都是准的)。

抓帧命令:
    v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=1 \\
             --stream-to=/tmp/frame.raw
文件格式: 16bit/像素小端, 低 10 位有效。
"""
import array
import os
import subprocess
import sys
import time

RAW = "/tmp/frame.raw"
DEVS = ("/dev/video0", "/dev/video1")


def grab(dev="/dev/video0", timeout=8):
    """抓一帧 raw。返回 (ok, msg, 文件大小)。"""
    try:
        os.remove(RAW)
    except OSError:
        pass
    for extra in (["--stream-mmap", "--stream-count=1"],
                  ["--stream-mmap=1", "--stream-count=1"]):
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", dev] + extra + ["--stream-to=" + RAW],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "抓帧超时", 0
        if os.path.exists(RAW) and os.path.getsize(RAW) > 0:
            return True, r.stderr.strip()[:100], os.path.getsize(RAW)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:150], 0
    return False, "没抓到数据", 0


def stats(path=RAW, stride=1):
    """统计 10bit 亮度分布。"""
    sz = os.path.getsize(path)
    n = sz // 2
    arr = array.array("H")
    with open(path, "rb") as f:
        arr.frombytes(f.read(n * 2))
    if sys.byteorder != "little":
        arr.byteswap()

    step = max(1, len(arr) // 300000)     # 采样上限 30 万点, 单核也够快
    vals = []
    over = 0
    dark = 0
    s = 0
    cnt = 0
    for i in range(0, len(arr), step):
        v = arr[i] & 0x3FF
        cnt += 1
        s += v
        if v >= 1000:
            over += 1
        if v < 64:
            dark += 1
        vals.append(v)
    vals.sort()
    m = len(vals)
    return {
        "mean": round(s / cnt, 1),
        "mean_pct": round(100.0 * (s / cnt) / 1023, 1),
        "p50": vals[m // 2],
        "p95": vals[int(m * 0.95)],
        "p99": vals[int(m * 0.99)],
        "max": vals[-1],
        "over_pct": round(100.0 * over / cnt, 2),   # 过曝像素比例
        "dark_pct": round(100.0 * dark / cnt, 2),   # 暗部比例
        "n": cnt,
        "bytes": sz,
    }


def measure(dev="/dev/video0"):
    ok, msg, sz = grab(dev)
    if not ok:
        return {"ok": False, "err": msg}
    try:
        st = stats()
        st["ok"] = True
        st["dev"] = dev
        return st
    except Exception as e:
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}


if __name__ == "__main__":
    import json
    dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/video0"
    for d in (dev,):
        r = measure(d)
        print(json.dumps(r, ensure_ascii=False))

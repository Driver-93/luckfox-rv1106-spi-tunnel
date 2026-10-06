#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测控制延迟。放到板子上跑, 避免 SSH 管道截断 heredoc。"""
import json
import sys
import time
import urllib.request


def sample(rounds=8, per=20):
    ts = []
    for _ in range(rounds):
        for _ in range(per):
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1/api/move",
                    data=json.dumps({"vx": 0.3, "vy": 0, "w": 0}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).read()
                ts.append((time.time() - t0) * 1000)
            except Exception:
                pass
            time.sleep(0.05)
        time.sleep(0.2)
    return ts


def stop():
    try:
        req = urllib.request.Request(
            "http://127.0.0.1/api/cmd",
            data=json.dumps({"c": "stop"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    ts = sample()
    ts.sort()
    stop()
    if not ts:
        print("  [%s] 没采到数据" % label)
        return
    n = len(ts)
    p50, p90, p99 = ts[n // 2], ts[int(n * 0.9)], ts[int(n * 0.99)]
    print("  [%s] n=%d  p50=%.1fms  p90=%.1fms  p99=%.1fms  max=%.1fms"
          % (label, n, p50, p90, p99, ts[-1]))
    if len(sys.argv) > 2:
        base = [float(x) for x in sys.argv[2].split()]
        print("       对比基线: p50 %+.1fms  p90 %+.1fms  p99 %+.1fms"
              % (p50 - base[0], p90 - base[1], p99 - base[2]))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印板子的 TCP 重传/收发计数 (跑两次做差, 就知道这段时间重传了多少)。"""
import subprocess


def parse(path, key):
    try:
        lines = [l.split() for l in open(path)]
    except Exception:
        return {}
    for i in range(0, len(lines) - 1, 2):
        if lines[i][0].rstrip(':') == key:
            return dict(zip(lines[i][1:], lines[i + 1][1:]))
    return {}


def frame():
    try:
        d = subprocess.run(["dmesg"], stdout=subprocess.PIPE,
                           timeout=5).stdout.decode("utf-8", "replace")
    except Exception:
        return "-"
    import re
    m = list(re.finditer(r"frames=(\d+) ok=(\d+) fail=(\d+) bad_magic=(\d+)", d))
    if not m:
        return "-"
    g = m[-1]
    return "frames=%s fail=%s" % (g.group(1), g.group(3))


t = parse("/proc/net/snmp", "Tcp")
e = parse("/proc/net/netstat", "TcpExt")
print("TCP   OutSegs=%s RetransSegs=%s InSegs=%s InErrs=%s" % (
    t.get("OutSegs"), t.get("RetransSegs"), t.get("InSegs"), t.get("InErrs")))
print("TCPExt RetransSegs=%s TCPLostRetransmit=%s SpuriousRetrans=%s" % (
    e.get("RetransSegs"), e.get("TCPLostRetransmit"), e.get("TCPSpuriousRTOs")))
print("SPI   %s" % frame())

#!/usr/bin/env python3
"""Temporary UDP listener used to prove that UDP survives the SPI tunnel.

mediamtx.yml disables WebRTC UDP with the comment "the SPI tunnel only
forwards TCP". The C5 firmware's FWD_UDP table says otherwise (8189/udp is
mapped). This listener settles it: if a datagram sent from the PC to
192.168.3.69:8189 arrives here, UDP is forwardable and WebRTC can drop
ICE/TCP.
"""
import socket
import sys
import time

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8189
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", port))
s.settimeout(0.5)

out = open("/tmp/udp_test.log", "w")
t0 = time.time()
n = 0
out.write("listening on udp/%d for %.0fs\n" % (port, secs))
out.flush()
while time.time() - t0 < secs:
    try:
        d, a = s.recvfrom(2048)
    except socket.timeout:
        continue
    n += 1
    out.write("recv %3d bytes from %s:%d  %r\n" % (len(d), a[0], a[1], d[:48]))
    out.flush()
    try:
        s.sendto(b"PONG-" + str(n).encode(), a)
    except Exception as e:
        out.write("reply failed: %s\n" % e)
        out.flush()
out.write("done: %d datagrams\n" % n)
out.close()
print("received %d" % n)

#!/usr/bin/env python3
"""Raw spidev cost: how long does ONE 4096-byte full-duplex transfer take?

Why: the pump runs at ~110 frames/s, i.e. 9.1ms per exchange. At 20MHz a 4096
byte frame is 1.64ms of wire time, so ~7.4ms is NOT wire time. This script
separates the two: it clocks frames with no protocol logic at all.

If this comes back near 1.7ms, the board side is fine and the missing 7.4ms is
the C5's per-transaction turnaround (re-arming its slave DMA between CS edges).
If it comes back near 9ms, the board's spidev path is the ceiling.
"""
import ctypes
import fcntl
import os
import struct
import sys
import time

FRAME = 4096
SPI_IOC_MESSAGE_1 = 0x40206b00
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046b04
SPI_IOC_RD_MAX_SPEED_HZ = 0x80046b04
SPI_IOC_WR_MODE = 0x40016b01
SPI_IOC_WR_BITS_PER_WORD = 0x40016b03


def wr(fd, req, val, n):
    fmt = "<B" if n == 1 else "<I"
    b = ctypes.create_string_buffer(struct.pack(fmt, val))
    fcntl.ioctl(fd, req, b, True)


def main():
    speed = int(sys.argv[1]) if len(sys.argv) > 1 else 20000000
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    fd = os.open("/dev/spidev0.0", os.O_RDWR)
    wr(fd, SPI_IOC_WR_MODE, 0, 1)
    wr(fd, SPI_IOC_WR_BITS_PER_WORD, 8, 1)
    wr(fd, SPI_IOC_WR_MAX_SPEED_HZ, speed, 4)
    rd = ctypes.create_string_buffer(4)
    fcntl.ioctl(fd, SPI_IOC_RD_MAX_SPEED_HZ, rd, True)
    actual = struct.unpack("<I", rd.raw)[0]

    tx = ctypes.create_string_buffer(FRAME)
    rx = ctypes.create_string_buffer(FRAME)
    txaddr = ctypes.addressof(tx)
    rxaddr = ctypes.addressof(rx)
    tr = struct.pack("<QQIIHBBI", txaddr, rxaddr, FRAME, actual, 0, 8, 0, 0)

    # warm up
    for _ in range(20):
        fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, tr, False)

    ts = []
    for _ in range(n):
        t0 = time.time()
        fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, tr, False)
        ts.append((time.time() - t0) * 1000.0)
    os.close(fd)

    ts.sort()
    wire = FRAME * 8.0 / actual * 1000.0
    print("speed=%d Hz  frames=%d" % (actual, n))
    print("wire time per frame (theoretical): %.3f ms" % wire)
    print("measured min  : %.3f ms  -> overhead %.3f ms" % (ts[0], ts[0] - wire))
    print("measured p50  : %.3f ms" % ts[len(ts) // 2])
    print("measured mean : %.3f ms" % (sum(ts) / len(ts)))
    print("measured max  : %.3f ms" % ts[-1])
    print("frames/s at p50: %.0f" % (1000.0 / ts[len(ts) // 2]))


main()

#!/usr/bin/env python3
"""
spinet.py -- Luckfox side: SPI tunnel driven by a real TUN interface.

What this replaced
------------------
The board's kernel shipped without CONFIG_TUN, so this process could not give
the board an IP address. Everything the board served had to be tunnelled
connection by connection instead: a reverse proxy on the C3 accepted the
browser, asked the board to dial 127.0.0.1:<port>, and both ends shovelled bytes
with a slot table capped at 8 concurrent connections. No UDP (so WebRTC media
could only use TCP), no retransmission, worker-task leaks, REVOK timing bugs,
plus an HTTP/SOCKS5/DNS userspace proxy stack for the board's own outbound
traffic -- all of it because the board had no IP.

tun.ko (cross-built against this exact kernel, vermagic
`5.10.160 mod_unload ARMv7 thumb2 p2v8`, loaded by /etc/init.d/S14tun) fixed the
root cause. The board is now an ordinary LAN host:

    browser --WiFi--> C3 (192.168.3.69)
                        |  NAPT + port map
                        v
                     spi0 (10.77.0.1)  ==  SPI tunnel, bare IP packets  ==
                        v
              board tun0 (10.77.0.2) --> normal TCP/UDP stack

So all the application-layer machinery above is gone, along with the 8-connection
limit. This file now does exactly three things:

  1. carry IP packets between tun0 and the C3          (frame type T_IP)
  2. remember the C3's status line for the web page     (frame type T_STAT)
  3. keep tun0 itself healthy                           (see tun_reassert)
"""
import ctypes, fcntl, json, os, signal, socket, struct, subprocess, sys, \
    threading, time, collections, zlib

# The board has ONE Cortex-A7 core. This loop busy-polls for the whole life of
# the process, and CPython's default 5ms switch interval would let it starve
# web_server.py's request threads. Yield much more eagerly.
sys.setswitchinterval(0.0005)

# ---------------- SPI link ----------------
FRAME, HDR = 4096, 16
MAGIC = 0x3254464C
T_HELLO, T_NODATA, T_STAT = 0x01, 0x03, 0x04
# Bare IP packet, one per frame. Must match TUN_T_IP in the C3's tunnel.h.
T_IP = 0x05

SPI_IOC_MAGIC = ord('k')
def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (t << 8) | nr
SPI_IOC_WR_MODE          = _IOC(1, SPI_IOC_MAGIC, 1, 1)
SPI_IOC_RD_MODE          = _IOC(2, SPI_IOC_MAGIC, 1, 1)
SPI_IOC_WR_BITS_PER_WORD = _IOC(1, SPI_IOC_MAGIC, 3, 1)
SPI_IOC_RD_BITS_PER_WORD = _IOC(2, SPI_IOC_MAGIC, 3, 1)
SPI_IOC_WR_MAX_SPEED_HZ  = _IOC(1, SPI_IOC_MAGIC, 4, 4)
SPI_IOC_RD_MAX_SPEED_HZ  = _IOC(2, SPI_IOC_MAGIC, 4, 4)
SPI_IOC_MESSAGE_1        = _IOC(1, SPI_IOC_MAGIC, 0, 32)

C3_STATE_FILE = "/tmp/c3_state.json"


def csum16(b):
    """Frame checksum. Must match tun_csum16() in the C3's tunnel.h.

    zlib.adler32 is implemented in C. Its low 16 bits are
    A = (1 + sum(bytes)) % 65521, which is exactly what the C3 computes.

    This replaced `sum(b) & 0xFFFF`. That is a byte-at-a-time loop in the
    interpreter: measured 0.550 ms for a 4056-byte frame, and every exchange
    computes it twice (payload sent + payload received). At 40MHz an exchange
    takes 2.1ms total, so the checksum was about half of it -- the single
    largest remaining cost on the link.
    """
    return zlib.adler32(b) & 0xFFFF


def _sh(cmd):
    """Best-effort shell command; returns (rc, output) and never raises."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=5)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return 1, str(e)


SPI_SPEED_FILE = "/userdata/spi_speed"


def spi_speed(default=24_000_000):
    """SPI clock for the tunnel, overridable via /userdata/spi_speed.

    The maximum reliable rate has to be found by measurement: 24MHz is what this
    ran at for a long time, but the per-exchange overhead (~2.4ms of the ~3.8ms
    round trip) dominates, so raising the clock only helps in proportion to the
    1.365ms of actual wire time. A file means a sweep costs a restart instead of
    an edit-and-deploy.
    """
    try:
        with open(SPI_SPEED_FILE) as f:
            v = int(f.read().strip())
            if v > 0:
                return v
    except Exception:
        pass
    return default


class SpiLink:
    # A 4096-byte frame at 10MHz takes 3.3ms, capping throughput at ~305
    # frames/s. 24MHz gives ~2.5x that and the C3 keeps up.
    def __init__(self, dev="/dev/spidev0.0", speed=24_000_000):
        self.fd = os.open(dev, os.O_RDWR)
        self._wr(SPI_IOC_WR_MODE, 0, 1)
        self._wr(SPI_IOC_WR_BITS_PER_WORD, 8, 1)
        self._wr(SPI_IOC_WR_MAX_SPEED_HZ, speed, 4)
        self.speed = struct.unpack("<I", self._rd(SPI_IOC_RD_MAX_SPEED_HZ, 4))[0]
        self.lock = threading.Lock()
        self.seq = 0
        self.last_seq = 0
        self.txbuf = ctypes.create_string_buffer(FRAME)
        self.rxbuf = ctypes.create_string_buffer(FRAME)
        # 从机上一轮声明的应答长度 (HDR + len)。用于决定本次传输多少字节:
        # 必须同时容纳"我要发的"和"从机要回的", 否则会把对方的数据截断。
        self._peer_len = HDR

    def _rd(self, req, n):
        b = ctypes.create_string_buffer(n)
        fcntl.ioctl(self.fd, req, b, True)
        return b.raw

    def _wr(self, req, val, n):
        fmt = "<B" if n == 1 else "<I"
        b = ctypes.create_string_buffer(struct.pack(fmt, val))
        fcntl.ioctl(self.fd, req, b, True)

    def exchange(self, ftype, payload=b"", seq=None):
        """Thread-safe single request/response cycle.

        seq: when set, retransmit this exact frame number.
        Returns (rtype, payload, ack16); ack16 is the slave's cumulative ack
        (low 16 bits of seq) from the frame header's reserved field.
        """
        if len(payload) > FRAME - HDR:
            payload = payload[:FRAME - HDR]
        with self.lock:
            if seq is None:
                self.seq = (self.seq + 1) & 0xFFFFFFFF
                seq = self.seq
            self.last_seq = seq
            ln = len(payload)
            h = struct.pack("<IBBHIHH", MAGIC, ftype, 0, 0, seq,
                            ln, csum16(payload) if ln else 0)
            # Write the header and payload straight into the DMA buffer; the
            # rest is padding the peer never reads (it trusts the length field),
            # so zeroing 4KB every cycle was pure waste.
            txaddr = ctypes.addressof(self.txbuf)
            ctypes.memmove(txaddr, h, HDR)
            if ln:
                ctypes.memmove(txaddr + HDR, payload, ln)
            # Transfer length: FIXED at FRAME.
            #
            # This used to be variable -- sized from _peer_len, the length the
            # slave announced in its previous header. That is reverted, and it
            # MUST stay reverted while the C5 runs fixed-length firmware:
            #
            #   SPI is full-duplex and clocked by the master, so ONE length
            #   governs both directions. A variable-length protocol needs the
            #   slave to tell the master how much to clock -- but `reserved`
            #   can only say "how much I will send back", not "how much I need
            #   you to send me". Sizing from the slave's reply length meant
            #   that whenever the slave answered with a 16-byte heartbeat, the
            #   master had no room for a 1350-byte IP packet and silently
            #   dropped it. Traffic deadlocked; err climbed ~1/s; ip tx/rx
            #   froze.
            #
            # Measured cost of fixed length: 4096 bytes at 20MHz = 1.64ms of
            # wire time per frame. It is real, but idle CPU headroom is ~33%,
            # so it is not worth a protocol change. A correct variable-length
            # scheme needs a SEPARATE master->slave length field (e.g. the
            # unused `seq`), with the slave taking max(own_tx, master_rx).
            #
            # The mismatch case is worth remembering: board variable-length +
            # C5 fixed-length produced fails climbing 1 per ~2 frames, 65-90%
            # ping loss, and ip rx frozen. Both sides must agree.
            want = FRAME
            tr = struct.pack("<QQIIHBBI",
                             txaddr,
                             ctypes.addressof(self.rxbuf),
                             want, self.speed, 0, 8, 0, 0)
            fcntl.ioctl(self.fd, SPI_IOC_MESSAGE_1, tr, False)
            # Peek the header, then copy only the announced bytes -- pulling the
            # whole 4KB rx buffer into a Python bytes object every frame was the
            # most expensive thing in this loop.
            rxaddr = ctypes.addressof(self.rxbuf)
            magic, rtype, _f, ack16, rseq, ln, cs = struct.unpack(
                "<IBBHIHH", ctypes.string_at(rxaddr, HDR))
            if magic != MAGIC or ln > FRAME - HDR:
                return None
            if not ln:
                return (rtype, b"", ack16)
            pl = ctypes.string_at(rxaddr + HDR, ln)
            if csum16(pl) != cs:
                return None
            return (rtype, pl, ack16)

    def close(self):
        os.close(self.fd)


# ---------------- TUN device ----------------

# MTU for tun0. MUST match SPINET_MTU in the C3's spinet.h.
#
# 1350 is chosen so exactly three packets fit in one SPI frame:
#     3 x 1350 + 3 x 2 length bytes = 4056  <= 4080 frame payload
# The link is stop-and-wait, so throughput is bytes-per-round-trip; packing
# three packets instead of one is worth about 2.8x. At 1400 only two fit
# (2 x 1402 = 2804).
TUN_MTU      = 1350
TUN_NAME     = "tun0"
TUN_LOCAL_IP = "10.77.0.2"
TUN_ADDR     = TUN_LOCAL_IP + "/24"
TUN_PEER     = "10.77.0.1"        # the C3's spi0 address
IP_CMD       = "/sbin/ip"

# _IOW('T', 202, int); the argument is a struct ifreq even though the encoded
# size is sizeof(int).
TUNSETIFF = 0x400454CA
IFF_TUN   = 0x0001
IFF_NO_PI = 0x1000                # no 4-byte packet-info prefix


def default_route_lines():
    """The routing-table lines that are actually default routes.

    Do NOT use `ip route show default` here. BusyBox's ip does not filter on
    that argument -- it prints the ENTIRE table. Verified on this board:

        # ip route show default
        10.77.0.0/24 dev tun0 scope link  src 10.77.0.2
        172.32.0.0/16 dev usb0 scope link  src 172.32.0.70
        192.168.3.64 dev tun0 scope link

    That made "is the output empty?" always false, so the tunnel never
    installed a default route even when there was none -- which is exactly the
    cable-out case this whole project is about. Parse the full table instead.
    """
    rc, out = _sh([IP_CMD, "route", "show"])
    return [l for l in out.splitlines() if l.split()[:1] == ["default"]]


class TunDev:
    """The board's own network interface, which is what makes all of this simple."""

    def __init__(self, name=TUN_NAME):
        # BusyBox's `ip` has no `tuntap` subcommand, so create it here.
        fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
        try:
            # struct ifreq is 40 bytes here; a short buffer lets the kernel read
            # past it.
            ifr = struct.pack("16sH22x", name.encode(), IFF_TUN | IFF_NO_PI)
            fcntl.ioctl(fd, TUNSETIFF, ifr)
        except OSError:
            # TUNSETIFF fails with EBUSY when another tun fd already owns this
            # name. Close ours or the fd leaks for the life of the process --
            # two /dev/net/tun fds were observed open at once for exactly this
            # reason.
            os.close(fd)
            raise
        self.fd = fd
        self.name = name

    def read(self):
        """One IP packet, or None if nothing is queued."""
        try:
            return os.read(self.fd, FRAME)
        except (BlockingIOError, OSError):
            return None

    def write(self, pkt):
        try:
            os.write(self.fd, pkt)
            return True
        except OSError:
            return False

    def addr_ok(self):
        """True when TUN_LOCAL_IP is actually assigned to this host.

        A UDP bind is the cheapest possible probe: the kernel refuses it with
        EADDRNOTAVAIL unless the address is local, so this needs no fork/exec.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((TUN_LOCAL_IP, 0))
            return True
        except OSError:
            return False
        finally:
            s.close()

    @staticmethod
    def loopback_ok():
        """True when 127.0.0.1 is assigned to lo.

        Without it the kernel has no `local 127.0.0.1` route, so every local
        connection matches the DEFAULT route and is sent out tun0, where it
        dies. That broke mediamtx -> rkipc (RTSP on 127.0.0.1:554) and the video
        reconnected forever, while external access kept working -- which made it
        look like a video bug rather than a networking one.

        It happened because spinet now starts (S22) BEFORE S40network runs
        /sbin/ifup -a, and busybox's ifup decided loopback was "already
        configured" and skipped it. S15loopback asserts it at boot; this is the
        runtime backstop.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", 0))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def addr_text(self):
        rc, out = _sh([IP_CMD, "addr", "show", "dev", self.name])
        return out

    def configure(self):
        """Address, MTU and routes. Returns notes for the log.

        Everything is verified by reading the interface back rather than by
        trusting the exit status: `ip addr add` was measured returning success
        on this board while the address did not stick. A tun0 that is UP with
        the right MTU but NO ADDRESS looks perfectly healthy in `ip link` while
        actually sending every packet out the default route instead.

        Routing: with the debug ethernet cable in, eth0 already owns the
        192.168.3.0/24 link route AND the default route. tun0 must not take
        either over, or the cable stops working. The default route is therefore
        only added when nothing else provides one -- i.e. exactly the cable-out
        case we actually care about.
        """
        notes = []
        want = TUN_LOCAL_IP

        _sh([IP_CMD, "link", "set", self.name, "up"])
        _sh([IP_CMD, "link", "set", self.name, "mtu", str(TUN_MTU)])

        applied = False
        last = ""
        for _ in range(6):
            if want in self.addr_text():
                applied = True
                break
            rc, last = _sh([IP_CMD, "addr", "add", TUN_ADDR, "dev", self.name])
            time.sleep(0.3)
        notes.append("addr %s OK" % TUN_ADDR if applied else
                     "!! addr %s NOT applied after 6 tries: %s" % (TUN_ADDR, last))

        # Reachability to the C3's spi0. "File exists" is normal right after
        # adding the address: the kernel creates the connected /24 route itself.
        _sh([IP_CMD, "route", "add", "10.77.0.0/24", "dev", self.name])

        defs = default_route_lines()
        if not defs:
            rc, out = _sh([IP_CMD, "route", "add", "default", "via", TUN_PEER,
                           "dev", self.name])
            notes.append("default route via %s" % TUN_PEER if rc == 0
                         else "!! default route FAILED: %s" % out)
        else:
            notes.append("default route left alone (%s)" % defs[0])

        return notes

    def clear_learned_routes(self):
        """Drop every /32 route that points at this device.

        Those are the only /32s we ever add, so this is a safe clean slate. It
        matters for recovery: while spinet is dead nothing expires them, so a
        stale route would keep the debug cable's path shadowed forever.
        """
        rc, out = _sh([IP_CMD, "route", "show", "dev", self.name])
        for line in out.splitlines():
            f = line.split()
            if f and f[0].endswith("/32"):
                _sh([IP_CMD, "route", "del", f[0], "dev", self.name])

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------- the tunnel ----------------

class Tunnel:
    def __init__(self, spi):
        self.spi = spi
        self.running = True
        self.stats = dict(frames=0, err=0)
        self.c3_state = {}          # status pushed by the C3, for the web page

        # ---- TUN ----
        self.tun = None
        self.ip_txq = collections.deque(maxlen=128)
        self.ip_tx = 0
        self.ip_rx = 0
        self.ip_drop = 0
        self.lan_peers = {}         # ip -> last time a packet arrived from it
        self.peer_idle = 120.0      # seconds before a learned route is withdrawn
        self._last_carrier = None   # eth0 link state
        self._nocarr = 0            # consecutive ticks without carrier
        self._nocarr_was = False    # carrier was missing on the previous tick
        self._rt_tick = 0           # housekeeping counter, for the 30s route check
        self._tun_retry = 0
        self._tun_err = None
        self._tun_lock = threading.Lock()

    # ---- TUN plumbing ----

    def try_setup_tun(self):
        """(Re)create tun0. Returns True on success and never raises.

        A failure here must NOT be fatal. This is the board's only network path:
        if spinet exits, the wireless link is gone completely and the board is
        unreachable over WiFi. That is exactly what happened once -- S14tun ran
        before /oem was mounted, so tun.ko never loaded at boot, and spinet
        dutifully exited on every restart, taking the whole wireless path with
        it while the hardware was perfectly fine.

        So: keep the SPI link alive, and retry from housekeeping() until the
        interface appears.

        Thread-safe, and that is not optional: this is called from both main()
        and the pump thread's housekeeping(). Two concurrent callers used to
        open /dev/net/tun twice, one won the name and the other hit EBUSY and
        then set self.tun = None -- clobbering the winner's object, so the
        interface was left created but unconfigured (no address, MTU 1500) and
        the next line died with AttributeError on None.
        """
        with self._tun_lock:
            if self.tun is not None:
                return True

            # Belt and braces: if the device node is missing entirely, try
            # loading the module ourselves. The init script should have done
            # this, but the failure mode is too costly to leave to one
            # boot-ordering assumption.
            if not os.path.exists("/dev/net/tun"):
                _sh(["/sbin/insmod", "/oem/usr/ko/tun.ko"])
                _sh(["insmod", "/oem/usr/ko/tun.ko"])

            try:
                t = TunDev()
            except OSError as e:
                self._tun_err = e
                return False

            # Only publish it once it is fully usable, so a concurrent caller
            # can never observe a half-built object.
            for n in t.configure():
                print("  [tun] %s" % n)
            t.clear_learned_routes()
            self.tun = t
            print("  [tun] %s up: %s <-> %s, mtu %d"
                  % (TUN_NAME, TUN_ADDR, TUN_PEER, TUN_MTU))
            return True

    def pack_ip_frame(self):
        """Pack as many queued IP packets as fit into ONE SPI frame.

        Layout: repeated [2B little-endian length][packet], matching tunnel.c.

        Why this matters so much: the link is stop-and-wait, so throughput is
        bytes-per-round-trip, and the round trip is ~10.5ms. Sending one
        1400-byte packet per 4080-byte frame gave 1400/10.5ms = 133 KB/s, which
        is exactly what was measured. Three 1350-byte packets per frame makes it
        ~380 KB/s for no extra protocol work.

        Built with a bytearray rather than struct.pack + list + join: this runs
        once per SPI exchange and the previous form allocated a list, a bytes
        object per length prefix, and a final concatenation.
        """
        buf = bytearray()
        limit = FRAME - HDR
        q = self.ip_txq
        while q:
            pkt = q[0]
            need = 2 + len(pkt)
            if len(buf) + need > limit:
                break
            q.popleft()
            buf += len(pkt).to_bytes(2, "little")
            buf += pkt
        return bytes(buf)

    def unpack_ip_frame(self, pl):
        """Split one T_IP frame payload into IP packets and inject each."""
        off = 0
        n = len(pl)
        first = True
        while off + 2 <= n:
            l = pl[off] | (pl[off + 1] << 8)
            off += 2
            if l < 20 or off + l > n:
                self.ip_drop += 1          # malformed; drop the remainder
                return
            pkt = pl[off:off + l]
            # Learn the peer once per frame, not per packet: every packet in a
            # frame arrives from the same link, and this runs on the hot path
            # (3 packets x ~300 frames/s means 900 dict lookups + a
            # socket.inet_ntoa per second for no extra information).
            if first:
                self.learn_peer(pkt)
                first = False
            if self.tun and self.tun.write(pkt):
                self.ip_rx += 1
            else:
                self.ip_drop += 1
            off += l

    def tun_poll(self):
        """Move packets waiting on tun0 into the send queue.

        At most 4 per iteration: draining everything would let one burst own the
        pump and push already-queued downstream frames further back.
        """
        if not self.tun:
            return False
        got = False
        for _ in range(4):
            pkt = self.tun.read()
            if not pkt:
                break
            if len(pkt) > TUN_MTU:
                self.ip_drop += 1        # the kernel should not produce these
                continue
            if len(self.ip_txq) >= self.ip_txq.maxlen:
                self.ip_drop += 1
                continue
            self.ip_txq.append(pkt)
            got = True
        return got

    def learn_peer(self, pkt):
        """Make replies to a host that reached us over the tunnel go back the same way.

        The board is multi-homed: eth0 carries the debug cable on 192.168.3.0/24
        and tun0 carries the real link. A browser at 192.168.3.64 reaches us
        THROUGH the tunnel, so its replies must leave through the tunnel -- but
        the main routing table sends 192.168.3.0/24 out eth0, and the browser
        then sees a reply from 10.77.0.2 to a connection it opened against
        192.168.3.69 and discards it. Measured: 8 connect attempts moved the
        board's ip rx by 84 while ip tx did not move at all.

        The kernel has no policy routing (CONFIG_IP_MULTIPLE_TABLES is off;
        `ip rule` returns "Operation not supported") and BusyBox's ip has no
        `table` keyword, so a source-based routing table is unavailable. The
        substitute is a /32 host route per peer: more specific than the /24, so
        it always wins, and the addresses are learned from the traffic itself
        rather than hardcoded.
        """
        if len(pkt) < 20:
            return
        src = socket.inet_ntoa(pkt[12:16])
        now = time.time()
        if src in self.lan_peers:
            self.lan_peers[src] = now
            return
        if src.startswith("10.77.") or len(self.lan_peers) >= 16:
            return
        self.lan_peers[src] = now
        for verb in ("replace", "add"):
            rc, out = _sh([IP_CMD, "route", verb, src + "/32", "dev", TUN_NAME])
            if rc == 0:
                print("  [tun] learned peer %s -> replies go via %s"
                      % (src, TUN_NAME))
                return
        print("  [tun] !! could not add host route for %s: %s" % (src, out))

    def reap_udhcpc_tun0(self):
        """Kill any `udhcpc -i tun0` and report whether one was killed.

        Why this is necessary (root cause of "切换分辨率后图传黑了"):

            rkipc (a Rockchip binary we cannot patch) spawns
            `udhcpc -i tun0 -T 1 -A 0 -b -q`. tun0 is point-to-point --
            there is never a DHCP server -- so it retries forever, and its
            discovery socket ends up on **0.0.0.0:554**, the port rkipc's
            RTSP server needs. Then rkipc starts, encodes fine, but cannot
            bind RTSP: RTSP DESCRIBE times out, mediamtx gets nothing, and
            the web player is black.

            This looked exactly like the old "stale socket after rkipc
            restart" bug and was misdiagnosed as such (video_ctl.py even
            told the user to reboot the board). It is a live process
            squatting on the port, not a stale socket.

        TIMING -- this is the important part, and I got it wrong first time:

            The obvious implementation (scan /proc, kill matches) costs
            **28 ms per call** on this board: 139 /proc entries times a
            readlink per fd, in Python, on ONE Cortex-A7. Calling that from
            `housekeeping()` -- which runs inside the pump loop, whose whole
            frame budget is 2-4 ms -- stalls the SPI pump for ~10 frames
            every 5 seconds. The C5 then sees an unarmed slave, status
            frames stop arriving, and the watchdog declares
            "tunnel WEDGED" and restarts spinet. Forever. That is a
            self-inflicted outage, and it is why this runs on a timer
            thread instead.

        So: called from a dedicated thread every REAP_PERIOD seconds, and
        it never touches the pump. Returns the list of killed pids.
        """
        killed = []
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().decode("utf-8", "replace")
                except OSError:
                    continue          # process exited mid-scan: normal
                if "udhcpc" in cmd and "tun0" in cmd:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                        killed.append(pid)
                    except OSError:
                        pass
        except OSError:
            pass
        if killed:
            print("  [tun] killed udhcpc on tun0 (pids %s) -- it holds "
                  "port 554, which blocks RTSP" % ",".join(killed))
        return killed

    @staticmethod
    def port554_holder():
        """Return the comm name of the process holding 0.0.0.0:554, or None.

        Resolved in ONE pass: build the fd->pid map first, then match the
        inode. Doing it as two separate snapshots names the WRONG process --
        between reading /proc/net/tcp and scanning /proc/*/fd the socket can
        be recycled, and PIDs are reused. That mistake made this bug look
        like a udhcpc-vs-rkipc mystery for several rounds.
        """
        # Build fd map once, then look up. ~13ms on this board.
        fdmap = {}
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                d = "/proc/%s/fd" % pid
                try:
                    fds = os.listdir(d)
                except OSError:
                    continue
                for fd in fds:
                    try:
                        t = os.readlink("%s/%s" % (d, fd))
                    except OSError:
                        continue
                    if t.startswith("socket:["):
                        fdmap[t[8:-1]] = pid
        except OSError:
            return None

        try:
            with open("/proc/net/tcp") as f:
                for line in f:
                    p = line.split()
                    # 022A = 554, state 0A = LISTEN
                    if len(p) > 9 and p[1].endswith(":022A") and p[3] == "0A":
                        pid = fdmap.get(p[9])
                        if pid is None:
                            return "unknown"
                        try:
                            with open("/proc/%s/comm" % pid) as g:
                                return "%s(pid=%s)" % (g.read().strip(), pid)
                        except OSError:
                            return "pid-%s" % pid
        except OSError:
            return None
        return None

    @staticmethod
    def eth_carrier():
        """True while the debug ethernet cable has a link.

        A plain file read, no subprocess -- cheap enough to call from the pump
        loop several times a minute.
        """
        try:
            with open("/sys/class/net/eth0/carrier") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def housekeeping(self):
        """Keep tun0 usable. Runs a few times a minute.

        Two jobs:

        1. Expire learned peer routes. They deliberately shadow eth0's
          192.168.3.0/24, which costs us the cable debug path -- the same PC
           has one address, so it can only have one path. Safety net: if the
           tunnel goes quiet for 120s it is broken, so withdraw the routes and
           the cable path comes back without a site visit. A reboot also
           restores it, since learned routes are not persisted.

        2. Re-assert the address. `rkipc` (a Rockchip binary we cannot patch)
           runs `udhcpc -i tun0`, and udhcpc's helper script begins with
           `ip addr flush` + `ip route flush`. tun0 is point-to-point, so there
           is never a DHCP server: it just destroys the configuration and
           retries forever. The helper is now guarded against tun0 as well, but
           this stays as the authoritative check.
        """
        if self.tun is None:
            # tun0 is missing. Retry every ~30s and keep the SPI link running:
            # exiting here would take the board's only network path down.
            self._tun_retry += 1
            if self._tun_retry % 6 == 1:
                # try_setup_tun() prints the full "tun0 up" line itself when it
                # actually creates the interface, so do not announce success
                # here too -- two lines for one event is how logs stop being
                # trustworthy.
                if not self.try_setup_tun():
                    print("  [tun] still no tun0 (%s); will keep retrying"
                          % self._tun_err)
            return

        now = time.time()

        # NOTE: the udhcpc/554 reaper deliberately does NOT run here.
        #
        # It costs 28 ms per scan (139 /proc entries, a readlink per fd, in
        # Python, on one Cortex-A7) while the pump's frame budget is 2-4 ms.
        # Running it in this loop stalled the SPI pump for ~10 frames every
        # 5s, which made the C5 miss status frames and the watchdog declare
        # "tunnel WEDGED" and restart spinet in a loop -- a self-inflicted
        # outage. It now runs on its own thread (see reaper_loop()).

        # Loopback is not optional: mediamtx reaches rkipc's RTSP over
        # 127.0.0.1, and web_server and sshd use it too. See loopback_ok().
        if not TunDev.loopback_ok():
            _sh([IP_CMD, "link", "set", "lo", "up"])
            _sh([IP_CMD, "addr", "add", "127.0.0.1/8", "dev", "lo"])
            print("  [net] !! 127.0.0.1 was missing on lo -- restored it")

        # --- the default route has to follow the cable -------------------
        #
        # The ethernet cable is ONLY a debug SSH channel; nothing the product
        # does may depend on it. So the board must end up with a working
        # default route on the tunnel alone.
        #
        # Two independent triggers, because neither is reliable by itself:
        #
        #   a) carrier is gone. Linux does NOT remove IPv4 routes on carrier
        #      loss (only on admin-down), so `default via 192.168.3.1 dev eth0`
        #      survives an unplug and every internet-bound packet is posted into
        #      a dead link. But some PHYs keep reporting carrier=1, so this
        #      alone can miss.
        #
        #   b) there is no default route AT ALL. Unambiguous, and it also covers
        #      the case where something else did remove eth0's routes.
        #
        # Runs once every 30s: one fork, and only when it might matter.
        #
        # NOTE: `ip link set eth0 down` is NOT a valid simulation of unplugging
        # the cable on this board -- the PHY keeps reporting carrier=1 because
        # the cable is still physically connected. Removing eth0's routes is the
        # accurate simulation, and trigger (b) is what catches it.
        carrier = self.eth_carrier()
        self._last_carrier = carrier
        self._rt_tick += 1
        if not carrier:
            self._nocarr += 1
        else:
            self._nocarr = 0

        need_check = (not carrier) or (self._rt_tick % 6 == 0)
        if need_check:
            defs = default_route_lines()
            has_tun = any(("dev %s" % TUN_NAME) in l for l in defs)
            has_eth = any("dev eth0" in l for l in defs)
            if not defs:
                # No default at all -> the tunnel is the only way out.
                rc, out = _sh([IP_CMD, "route", "add", "default",
                               "via", TUN_PEER, "dev", TUN_NAME])
                print("  [tun] no default route -> installed via %s: %s"
                      % (TUN_NAME, "ok" if rc == 0 else out))
            elif not carrier and not has_tun:
                # Cable is out but its (now dead) default is still in the
                # table. Take it out and hand the job to the tunnel.
                if has_eth:
                    _sh([IP_CMD, "route", "del", "default", "dev", "eth0"])
                rc, out = _sh([IP_CMD, "route", "add", "default",
                               "via", TUN_PEER, "dev", TUN_NAME])
                print("  [tun] cable out -> default route moved to %s: %s"
                      % (TUN_NAME, "ok" if rc == 0 else out))
            elif carrier and self._nocarr_was:
                # Cable is back: let eth0 own the default again.
                if has_tun:
                    _sh([IP_CMD, "route", "del", "default", "via", TUN_PEER,
                         "dev", TUN_NAME])
                    print("  [tun] cable back -> default route returned to eth0")
        self._nocarr_was = bool(self._nocarr)

        for ip in [i for i, t in self.lan_peers.items() if now - t > self.peer_idle]:
            _sh([IP_CMD, "route", "del", ip + "/32", "dev", TUN_NAME])
            del self.lan_peers[ip]
            print("  [tun] peer %s went quiet, withdrew its host route" % ip)

        if self.tun.addr_ok():
            return
        print("  [tun] !! %s vanished, reconfiguring" % TUN_LOCAL_IP)
        for n in self.tun.configure():
            print("  [tun] %s" % n)

    # ---- main loop ----

    def pump(self):
        """Drive the SPI link.

        The slave answers with one frame of latency: it processes frame N and
        delivers the result in the reply to frame N+1. The master must therefore
        keep exchanging frames, or nothing from the C3 ever comes back.

        Reliable board->C3 send, now a real sliding window.
         ---------------------------------------------------
        The C3 ACKs every well-checksummed T_IP frame cumulatively (low 16 bits
        of seq, in the reply header's reserved field) and dedups retransmits by
        seq.

        This used to be stop-and-wait with SEQ_JUMP=400, because the C3's dedup
        bitmap slid by whole bytes for a bit-granular shift and therefore
        reported never-seen frames as duplicates. Jumping 400 forced the C3 down
        its "too far ahead" resync branch, which bypassed the bitmap -- but it
        also meant only ONE frame could ever be in flight. Measured: 137 KB/s,
        which is exactly one 1400-byte packet per 10.5ms round trip.

        tunnel.c now uses a small mask window that shifts correctly, so frames
        are numbered sequentially and up to WINDOW of them can be in flight.

        Why that is the whole ballgame: throughput = payload-per-frame x
        frames-in-flight / RTT. Packing three packets per frame took 137 -> 377
        KB/s; allowing a window takes it to the wire limit instead of the
        round-trip limit.
        """
        outbox = {}              # seq32 -> [payload, last_send_time]
        WINDOW = 16              # frames allowed in flight (C3 ACK_WIN is 64)
        RETRY_AFTER = 0.15       # s: unacked for this long -> resend
        ack16 = None
        ack16_seen = None            # last ack we actually processed
        retx_count = 0
        last_c3_up = 0
        last_housekeeping = 0.0
        last_progress = time.time()   # when the window last advanced

        rate_t0 = time.time()
        rate_n = 0
        rate_bytes = 0
        idle_streak = 0

        def _acked16(seq32, ack16):
            # ack16 is the C3's cumulative ack. With a window of 16 and
            # sequential numbering, a frame is acked iff ack16 has caught up to
            # it; the modular difference is then small and positive, and wraps
            # to a large value when it has not.
            return ((ack16 - (seq32 & 0xFFFF)) & 0xFFFF) < 64

        while self.running:
            # Only re-scan the outbox when the C3's cumulative ack actually
            # moved. Rebuilding the list and calling _acked16() on all 16
            # in-flight frames every iteration was ~16 interpreter calls per
            # exchange, on the hottest loop in the process.
            if ack16 is not None and ack16 != ack16_seen:
                ack16_seen = ack16
                before = len(outbox)
                for s in [s for s in outbox if _acked16(s, ack16)]:
                    del outbox[s]
                if len(outbox) < before:
                    last_progress = time.time()

            # Stall guard.
            #
            # The C3 drops anything more than ACK_WIN (64) sequences ahead of
            # its cumulative ack, and only a HELLO resets that. If the two ever
            # drift apart -- a long probe run, a lost burst, a C3 reboot that
            # keeps its s_ack_seq -- the window fills and nothing can ever
            # advance again. Detect that and re-handshake, which resyncs both
            # sides to a known sequence.
            if len(outbox) >= WINDOW and (time.time() - last_progress) > 3.0:
                print("  [pump] window stalled (16 unacked for 3s) -- "
                      "re-handshaking to resync")
                ok = False
                for _ in range(20):
                    hr = self.spi.exchange(T_HELLO, b"")
                    if hr and hr[0] == T_HELLO:
                        ok = True
                        break
                    time.sleep(0.05)
                if ok:
                    with self.spi.lock:
                        self.spi.seq = self.spi.last_seq
                    outbox.clear()
                    ack16 = None
                    ack16_seen = None
                    last_progress = time.time()
                    self.stats["stalls"] = self.stats.get("stalls", 0) + 1
                    print("  [pump] resynced at seq=%d" % self.spi.seq)
                else:
                    last_progress = time.time()   # do not spin on this

            # Read tun0 every iteration: a non-blocking read on an empty fd is
            # one cheap syscall, and skipping it would add a frame of latency to
            # control input.
            self.tun_poll()

            now = time.time()
            if now - last_housekeeping >= 5.0:
                last_housekeeping = now
                self.housekeeping()

            send_seq = None
            payload = b""

            # 1) Retransmit the OLDEST overdue frame. Retransmitting the newest
            #    instead would livelock: the oldest gap never fills, so the C3's
            #    cumulative ack never advances past it.
            if outbox:
                oldest = next(iter(outbox.items()))
                if now - oldest[1][1] >= RETRY_AFTER:
                    send_seq, payload = oldest[0], oldest[1][0]

            # 2) Otherwise send fresh work while the window has room.
            if send_seq is None and len(outbox) < WINDOW:
                payload = self.pack_ip_frame()

            if send_seq is not None:
                r = self.spi.exchange(T_IP, payload, seq=send_seq)
                if send_seq in outbox:
                    outbox[send_seq][1] = time.time()
                retx_count += 1
            elif payload:
                with self.spi.lock:
                    self.spi.seq = (self.spi.seq + 1) & 0xFFFFFFFF
                    s = self.spi.seq
                r = self.spi.exchange(T_IP, payload, seq=s)
                outbox[s] = [payload, time.time()]
                self.ip_tx += 1
            else:
                # Idle probe.
                #
                # MUST NOT consume a sequence number. The C3 only advances its
                # cumulative ack for T_IP frames, so every probe widens the gap
                # between this counter and the C3's s_ack_seq. Measured: the
                # pump starts before tun0 exists and spends 1-2s emitting only
                # probes, which pushed spi.seq ~400 ahead; the first data frame
                # then landed outside the C3's 64-entry window, was rejected as
                # "too far ahead", and because the window never advanced, every
                # subsequent frame was rejected too. Result: ip tx stuck at 16,
                # retx climbing forever, ping dead.
                #
                # The old stop-and-wait code hid this: SEQ_JUMP forced a full
                # resync on every data frame, so drift never accumulated.
                r = self.spi.exchange(T_NODATA, b"", seq=self.spi.seq)

            if r and len(r) > 2:
                ack16 = r[2]

            self.stats["frames"] += 1
            rate_n += 1
            rate_bytes += len(payload)

            if rate_n % 200 == 0:
                dt = time.time() - rate_t0
                if dt > 0:
                    try:
                        _up = float(open("/proc/uptime").read().split()[0])
                    except Exception:
                        _up = -1.0
                    # The uptime stamp is here because boot-time diagnosis is
                    # otherwise guesswork: it makes it obvious when the link
                    # actually started carrying traffic, separately from when
                    # tun0 merely existed.
                    print("  [pump] t=%6.1fs  %.0f frames/s, %.1f KB/s up "
                          "(fails=%d, retx=%d, ip tx=%d rx=%d drop=%d)"
                          % (_up, rate_n / dt, rate_bytes / dt / 1024,
                             self.stats["err"], retx_count,
                             self.ip_tx, self.ip_rx, self.ip_drop))
                rate_t0 = time.time()
                rate_n = 0
                rate_bytes = 0

            got_work = False
            if not r:
                self.stats["err"] += 1
            else:
                rtype, pl = r[0], r[1]
                if rtype == T_STAT:
                    got_work = True
                    rebooted = False
                    try:
                        txt = pl.decode("utf-8", "ignore")
                        self.c3_state = json.loads(txt)
                        with open(C3_STATE_FILE, "w") as f:
                            f.write(txt)
                        up = int(self.c3_state.get("up_ms", 0))
                        # up_ms going backwards means the C3 rebooted. Its ACK
                        # window starts from scratch while we still hold stale
                        # seq state, so every frame we send looks like an
                        # ancient duplicate until we re-handshake.
                        if up and last_c3_up and up + 60000 < last_c3_up:
                            rebooted = True
                        if up:
                            last_c3_up = up
                    except Exception:
                        pass
                    if rebooted:
                        print("  [pump] C3 rebooted (up_ms went backwards), "
                              "re-handshaking ...")
                        ok = False
                        for _ in range(20):
                            hr = self.spi.exchange(T_HELLO, b"")
                            if hr and hr[0] == T_HELLO:
                                ok = True
                                break
                            time.sleep(0.05)
                        if ok:
                            # Continue T_IP frames from the HELLO's seq so the
                            # C3's fresh cumulative ack has no hole at the start.
                            with self.spi.lock:
                                self.spi.seq = self.spi.last_seq
                            outbox.clear()
                            print("  [pump] re-handshake done")
                        else:
                            print("  [pump] !! re-handshake failed, will retry")
                elif rtype == T_IP and pl:
                    # A frame can carry several IP packets; see pack_ip_frame()
                    # for the layout and why it matters.
                    got_work = True
                    self.unpack_ip_frame(pl)

            if not payload and not got_work:
                idle_streak += 1
                if idle_streak >= 2:
                    # Escalating idle backoff. 1ms keeps worst-case added
                    # latency ~1 frame while giving rkipc/mediamtx/web_server
                    # the CPU they need -- but 1ms alone still polls at ~230
                    # probes/s (each round trip is ~3.4ms of wire time), which
                    # burned ~12% CPU 24/7 whenever nobody was watching the
                    # video. So keep escalating: after sustained true idle the
                    # probe rate decays toward ~75/s (8ms), and ANY real work
                    # (an IP packet to send, T_IP/T_STAT in the reply) resets
                    # to full speed immediately. Worst-case added latency for
                    # the first control packet after idle is <= 8ms on top of
                    # ~28ms RTT. While video streams the queue never empties,
                    # so this path never runs.
                    _d = 0.001
                    if idle_streak >= 30:
                        _d = 0.008
                    elif idle_streak >= 10:
                        _d = 0.004
                    time.sleep(_d)
            else:
                idle_streak = 0


def main():
    print("=" * 56)
    print(" Luckfox SPI tunnel over TUN (no ethernet needed)")
    print("=" * 56)

    spi = SpiLink("/dev/spidev0.0", speed=spi_speed())
    print("  SPI /dev/spidev0.0 @ %d Hz" % spi.speed)

    # The slave answers with one frame of latency, so the first exchanges after
    # opening the device can still carry the previous round's reply.
    #
    # WAIT FOR THE C3 PROPERLY. This used to give up after 10 tries (0.5s).
    # spinet now starts early in boot, before the ESP has finished associating
    # with WiFi, so a short timeout meant it exited and the watchdog had to
    # restart it over and over -- that restart loop, not the ESP itself, was
    # what made the board unreachable for ~30s after a reboot.
    print("  waiting for the C3 ...")
    hello_ok = False
    t0 = time.time()
    last_note = 0.0
    while time.time() - t0 < 120.0:
        r = spi.exchange(T_HELLO, b"")
        if r and r[0] == T_HELLO:
            hello_ok = True
            break
        waited = time.time() - t0
        if waited - last_note >= 5.0:
            last_note = waited
            print("  ... still waiting for the C3 (%.0fs)" % waited)
        time.sleep(0.2)
    if not hello_ok:
        print("  !! C3 no reply after 120s -- check SPI wiring and C3 power")
        return 1
    print("  C3 online (%.1fs)" % (time.time() - t0))
    # The slave resynced its ACK window to the last HELLO's seq. Continue from
    # the next seq so the cumulative ack is not stuck on a hole.
    with spi.lock:
        spi.seq = spi.last_seq

    tun = Tunnel(spi)

    # The pump MUST start before anything that needs the tunnel -- including the
    # self-test, which pings the C3 across it. Frames only move while the pump
    # is running, so running the self-test first is guaranteed to fail and also
    # pushes the link's availability back by the whole retry loop. The pump is
    # safe to start this early: tun_poll() and housekeeping() both cope with
    # tun0 not existing yet.
    threading.Thread(target=tun.pump, daemon=True).start()

    # The udhcpc/554 reaper runs on its OWN thread, never in the pump.
    #
    # It costs 28 ms per scan on this board; the pump's frame budget is
    # 2-4 ms. Calling it from housekeeping() (inside the pump) stalled the
    # SPI link for ~10 frames every 5s, which tripped the watchdog into
    # restarting spinet in a loop. See Tunnel.reap_udhcpc_tun0().
    #
    # 20s period: rkipc only spawns udhcpc when it (re)starts -- i.e. on a
    # resolution switch -- so this only has to be fast enough to free 554
    # before the user notices a black player. A 28 ms scan every 20 s is
    # 0.14% of one core, which is affordable; every 5 s inside the pump was
    # not.
    def reaper_loop():
        while True:
            time.sleep(20)
            try:
                tun.reap_udhcpc_tun0()
            except Exception as e:
                print("  [tun] reaper error: %s" % e)

    threading.Thread(target=reaper_loop, daemon=True).start()

    # A failure here is NOT fatal. Exiting would remove the board's only network
    # path and make it unreachable over WiFi -- the hardware is fine, only the
    # interface is missing, and housekeeping() will keep retrying.
    if tun.try_setup_tun():
        # TTL 1 is deliberate. Without a route for 10.77.0.0/24 the packet is
        # handed to the default gateway instead, and that router answers -- an
        # earlier "successful" ping proved nothing for exactly that reason.
        # A directly connected host answers a TTL-1 packet; a routed one cannot.
        print("\n  self-test: board -> C3 spi0 (%s), TTL=1" % TUN_PEER)
        rc, out = _sh([IP_CMD, "route", "get", TUN_PEER])
        print("  route: %s" % out)

        # A couple of retries: right after boot the C3 may still be associating
        # with WiFi. One attempt there produces a spurious "TUN link is down" in
        # the log, which is worse than useless when reading the log later to
        # diagnose a real problem.
        rc = 1
        for attempt in range(3):
            rc, out = _sh(["ping", "-c", "2", "-W", "2", "-t", "1", TUN_PEER])
            if rc == 0:
                break
            if attempt < 2:
                time.sleep(2)
        print("  %s" % (out.replace("\n", "\n  ") if out else "no ping output"))
        if rc == 0:
            print("  TUN link OK -- the board has a real IP now")
        else:
            print("  !! TUN link is down (the tunnel keeps running and will "
                  "recover; see the [pump] lines below)")
    else:
        print("  !! tun0 unavailable (%s)" % tun._tun_err)
        print("  !! the SPI link will keep running and retry every ~30s;")
        print("  !! wireless access returns as soon as the interface appears")

    print("\n  Ctrl-C to quit")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        tun.running = False
        spi.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

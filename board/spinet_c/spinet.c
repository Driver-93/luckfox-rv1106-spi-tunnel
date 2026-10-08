/*
 * spinet.c -- Luckfox side: SPI tunnel driven by a real TUN interface.
 *
 * Faithful C port of spinet.py (the Python user-space tunnel). Why C:
 * the pump's per-frame interpreter overhead (~0.5-0.6 ms in CPython) is
 * ~25% of the single Cortex-A7 under streaming load; the same logic in C
 * costs ~20-60 us per frame. Every protocol detail is ported 1:1 from
 * spinet.py -- the comments there explain WHY; this file only carries
 * short pointers back to the non-obvious rules.
 *
 * Protocol (must match the C5's tunnel.h / tunnel.c):
 *   frame = 4096 B fixed, header 16 B:
 *     u32 magic=0x3254464C, u8 type, u8 flags=0, u16 reserved(ack16),
 *     u32 seq, u16 len, u16 csum
 *   csum16 = adler32 low 16 bits = (1 + sum(bytes)) % 65521
 *   types: 1=HELLO 2=APP(unused) 3=NODATA 4=STAT 5=IP
 *   T_IP payload = repeated [u16 LE len][packet]
 *   slave ACKs T_IP/APP cumulatively (low 16 bits of seq) and dedups by seq.
 *
 * Non-obvious rules carried over from spinet.py (do not "simplify" these):
 *   * idle NODATA probes MUST NOT consume a sequence number
 *   * the slave answers with one frame of latency: keep exchanging
 *   * a frame's retransmit must reuse the SAME seq
 *   * after any HELLO resync: seq = last_seq, outbox cleared
 *   * learn_peer is applied once per frame (first packet only)
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <dirent.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <linux/spi/spidev.h>
#include <linux/if.h>
#include <sys/ioctl.h>

/* ---------------- constants ---------------- */
#define FRAME        4096
#define HDR          16
#define MAX_PAYLOAD  (FRAME - HDR)
#define MAGIC        0x3254464CL
#define T_HELLO      0x01
#define T_NODATA     0x03
#define T_STAT       0x04
#define T_IP         0x05

#define TUN_MTU      1350
#define TUN_NAME     "tun0"
#define TUN_LOCAL_IP "10.77.0.2"
#define TUN_ADDR     "10.77.0.2/24"
#define TUN_PEER     "10.77.0.1"
#define IP_CMD       "/sbin/ip"

#define WINDOW       16          /* frames in flight (C5 ACK_WIN is 64) */
#define RETRY_AFTER  0.15        /* s */
#define TXQ_MAX      128         /* queued IP packets */
#define NPEERS       16
#define PEER_IDLE    120.0       /* s before a learned route is withdrawn */

#define SPI_SPEED_FILE "/userdata/spi_speed"

#define C3_STATE_FILE "/tmp/c3_state.json"

/* TUNSETIFF = _IOW('T', 202, int) */
#define TUNSETIFF    0x400454CA
#define IFF_TUN      0x0001
#define IFF_NO_PI    0x1000

static volatile sig_atomic_t g_run = 1;
static void on_term(int sig) { (void)sig; g_run = 0; }

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void logln(const char *s)
{
    printf("  %s\n", s);
    fflush(stdout);
}

/* ---------------- shell helper ---------------- */
static void sh(const char *cmd)
{
    int rc = system(cmd);
    (void)rc;
}

/* Run command, return 0 on success. Output discarded (best-effort). */
static int sh_rc(const char *cmd)
{
    int rc = system(cmd);
    return rc == -1 ? 1 : WEXITSTATUS(rc);
}

/* ---------------- adler16 ---------------- */
static uint16_t csum16(const uint8_t *b, int n)
{
    uint32_t a = 1;
    int i;
    for (i = 0; i < n; i++) {
        a += b[i];
        if (a >= 65521) a -= 65521;
    }
    return (uint16_t)a;
}

/* ---------------- SPI link ---------------- */
static int spi_fd = -1;
static uint32_t spi_seq = 0;      /* last consumed seq */
static uint32_t spi_last_seq = 0; /* seq of the last sent frame */

/* DMA-safe (kmalloc-equivalent not needed in user space; bounce via
 * aligned buffers). tx/rx frame buffers. */
static uint8_t txbuf[FRAME];
static uint8_t rxbuf[FRAME];

static int spi_open(const char *dev, uint32_t speed)
{
    uint8_t mode = 0, bits = 8;
    spi_fd = open(dev, O_RDWR);
    if (spi_fd < 0) return -1;
    ioctl(spi_fd, SPI_IOC_WR_MODE, &mode);
    ioctl(spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    ioctl(spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);
    return 0;
}

static uint32_t spi_speed(void)
{
    FILE *f = fopen(SPI_SPEED_FILE, "r");
    if (f) {
        int v = 0;
        if (fscanf(f, "%d", &v) == 1 && v > 0) { fclose(f); return (uint32_t)v; }
        fclose(f);
    }
    return 24000000;
}

/* Build header into dst (16 B). */
static void build_hdr(uint8_t *dst, uint8_t type, uint32_t seq,
                      uint16_t len, uint16_t cs)
{
    dst[0] = MAGIC & 0xFF; dst[1] = (MAGIC >> 8) & 0xFF;
    dst[2] = (MAGIC >> 16) & 0xFF; dst[3] = (MAGIC >> 24) & 0xFF;
    dst[4] = type; dst[5] = 0;
    dst[6] = 0; dst[7] = 0;                 /* reserved (ack from slave on rx) */
    dst[8] = seq & 0xFF; dst[9] = (seq >> 8) & 0xFF;
    dst[10] = (seq >> 16) & 0xFF; dst[11] = (seq >> 24) & 0xFF;
    dst[12] = len & 0xFF; dst[13] = (len >> 8) & 0xFF;
    dst[14] = cs & 0xFF; dst[15] = (cs >> 8) & 0xFF;
}

/* One full-duplex exchange. seq: value to put in the header.
 * Returns rx payload length (>=0), or -1 if the reply had bad magic/len.
 * rx_type/rx_ack/rx_csum_bad are optional outputs. */
static int spi_exchange(uint8_t type, const uint8_t *payload, int plen,
                        uint32_t seq, uint8_t *rx_type, uint16_t *rx_ack,
                        int *rx_csum_bad)
{
    struct spi_ioc_transfer tr;
    uint16_t cs = 0;
    uint32_t magic;
    uint16_t rlen, rcs;
    uint8_t rtype;

    if (plen < 0) plen = 0;
    if (plen > MAX_PAYLOAD) plen = MAX_PAYLOAD;
    if (plen) cs = csum16(payload, plen);
    build_hdr(txbuf, type, seq, (uint16_t)plen, cs);
    if (plen) memcpy(txbuf + HDR, payload, plen);

    memset(&tr, 0, sizeof(tr));
    tr.tx_buf = (unsigned long)txbuf;
    tr.rx_buf = (unsigned long)rxbuf;
    tr.len = FRAME;
    tr.speed_hz = 0;             /* already set via WR_MAX_SPEED_HZ */
    tr.bits_per_word = 8;
    if (ioctl(spi_fd, SPI_IOC_MESSAGE(1), &tr) < 0) {
        /* A transfer error here reads back whatever the buffer held; treat
         * like a bad-magic reply so the caller counts an error. */
        if (rx_csum_bad) *rx_csum_bad = 0;
        if (rx_type) *rx_type = 0;
        if (rx_ack) *rx_ack = 0;
        return -1;
    }

    magic = (uint32_t)rxbuf[0] | ((uint32_t)rxbuf[1] << 8) |
            ((uint32_t)rxbuf[2] << 16) | ((uint32_t)rxbuf[3] << 24);
    rtype = rxbuf[4];
    uint16_t rack = (uint16_t)(rxbuf[6] | (rxbuf[7] << 8));
    rlen = (uint16_t)(rxbuf[12] | (rxbuf[13] << 8));
    rcs = (uint16_t)(rxbuf[14] | (rxbuf[15] << 8));

    if (rx_type) *rx_type = rtype;
    if (rx_ack) *rx_ack = rack;
    if (rx_csum_bad) *rx_csum_bad = 0;

    if (magic != MAGIC || rlen > MAX_PAYLOAD) return -1;
    if (rlen == 0) return 0;
    if (csum16(rxbuf + HDR, rlen) != rcs) {
        if (rx_csum_bad) *rx_csum_bad = 1;
        return -1;
    }
    return rlen;
}

/* ---------------- TUN ---------------- */
static int tun_fd = -1;

static int tun_open(void)
{
    struct ifreq ifr;
    int fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK);
    if (fd < 0) return -1;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, TUN_NAME, IFNAMSIZ - 1);
    ifr.ifr_flags = IFF_TUN | IFF_NO_PI;
    if (ioctl(fd, TUNSETIFF, &ifr) < 0) {
        close(fd);               /* EBUSY: another fd owns tun0 */
        return -1;
    }
    return fd;
}

static int tun_read(uint8_t *buf, int maxlen)
{
    int n = (int)read(tun_fd, buf, maxlen);
    return n;                     /* <0 with EAGAIN when empty */
}

static int tun_write(const uint8_t *pkt, int n)
{
    return write(tun_fd, pkt, n);
}

/* ---------------- queues ---------------- */
static uint8_t txq[TXQ_MAX][1500];
static int txq_len[TXQ_MAX];
static int txq_head = 0, txq_cnt = 0;   /* FIFO ring */
static int txq_full = 0;

/* Small-packet priority queue (ported from the kernel version's design):
 * control/ACK packets (<= PRIO_MAX bytes) must not wait behind 1350B video
 * packets. Under streaming, a FIFO tun queue adds 50-200ms to every control
 * round trip (measured RTT 216 ms while video ran); with priority the
 * control packet rides in the very next SPI frame. */
#define PRIO_MAX     128
#define PRIO_SLOTS   64
static uint8_t txq_prio[PRIO_SLOTS][PRIO_MAX];
static int txqp_len[PRIO_SLOTS];
static int txqp_head = 0, txqp_cnt = 0;

static void txq_push(const uint8_t *pkt, int n)
{
    int tail;
    if (n <= PRIO_MAX) {
        if (txqp_cnt >= PRIO_SLOTS) { txq_full++; return; }
        tail = (txqp_head + txqp_cnt) % PRIO_SLOTS;
        memcpy(txq_prio[tail], pkt, n);
        txqp_len[tail] = n;
        txqp_cnt++;
        return;
    }
    if (txq_cnt >= TXQ_MAX) { txq_full++; return; }
    tail = (txq_head + txq_cnt) % TXQ_MAX;
    memcpy(txq[tail], pkt, n);
    txq_len[tail] = n;
    txq_cnt++;
}

static int txq_pop(uint8_t *dst)
{
    int n;
    if (txqp_cnt > 0) {
        n = txqp_len[txqp_head];
        memcpy(dst, txq_prio[txqp_head], n);
        txqp_head = (txqp_head + 1) % PRIO_SLOTS;
        txqp_cnt--;
        return n;
    }
    if (txq_cnt == 0) return 0;
    n = txq_len[txq_head];
    memcpy(dst, txq[txq_head], n);
    txq_head = (txq_head + 1) % TXQ_MAX;
    txq_cnt--;
    return n;
}

/* ---------------- outbox (sliding window) ---------------- */
typedef struct {
    int used;
    uint32_t seq;
    int plen;
    double last_send;
    unsigned long long ord;   /* insertion order -- see outbox_oldest() */
    uint8_t payload[MAX_PAYLOAD];
} outbox_ent;

static outbox_ent outbox[WINDOW];
static unsigned long long g_ord = 0;

static outbox_ent *outbox_find(uint32_t seq)
{
    int i;
    for (i = 0; i < WINDOW; i++)
        if (outbox[i].used && outbox[i].seq == seq) return &outbox[i];
    return NULL;
}

/* Oldest = SMALLEST INSERTION ORDER, not lowest slot index.
 *
 * spinet.py used a dict, which iterates in insertion order. This array
 * reuses freed slots: after a partial ack frees slots 0..2, the next
 * inserts (seq 17,18,19) land there while seq 4..16 still sit in slots
 * 3..15 -- at that point slot 0 holds the NEWEST frame. Retransmitting
 * "slot 0" then retransmits the newest frame, the hole at seq 4 never
 * fills, the C5's cumulative ack freezes, the window wedges and the stall
 * guard re-handshakes every 3 s (measured: 103 stalls = the reported
 * video stutter). Exactly the livelock spinet.py's comment warns about. */
static outbox_ent *outbox_oldest(void)
{
    int i;
    outbox_ent *best = NULL;
    for (i = 0; i < WINDOW; i++)
        if (outbox[i].used && (!best || outbox[i].ord < best->ord))
            best = &outbox[i];
    return best;
}

static outbox_ent *outbox_add(uint32_t seq, const uint8_t *payload, int plen,
                              double t)
{
    int i;
    for (i = 0; i < WINDOW; i++) {
        if (!outbox[i].used) {
            outbox[i].used = 1;
            outbox[i].seq = seq;
            outbox[i].plen = plen;
            outbox[i].last_send = t;
            outbox[i].ord = ++g_ord;
            memcpy(outbox[i].payload, payload, plen);
            return &outbox[i];
        }
    }
    return NULL;
}

static int outbox_count(void)
{
    int i, n = 0;
    for (i = 0; i < WINDOW; i++) if (outbox[i].used) n++;
    return n;
}

static void outbox_clear(void)
{
    memset(outbox, 0, sizeof(outbox));
}

static int acked16(uint32_t seq32, uint16_t ack16)
{
    return (int)(((ack16 - (seq32 & 0xFFFF)) & 0xFFFF) < 64);
}

/* ---------------- learned peers ---------------- */
static struct { uint32_t ip; double t; } lan_peers[NPEERS];
static int nlan = 0;

static void learn_peer(const uint8_t *pkt)
{
    uint32_t src;
    int i;
    char cmd[128], ip[20];
    double now = now_s();
    memcpy(&src, pkt + 12, 4);
    for (i = 0; i < nlan; i++) {
        if (lan_peers[i].ip == src) { lan_peers[i].t = now; return; }
    }
    /* skip 10.77.* and cap */
    if ((src & 0xFF) == 10 && ((src >> 8) & 0xFF) == 77) return;
    if (nlan >= NPEERS) return;
    {
        uint8_t a = pkt[12], b = pkt[13], c = pkt[14], d = pkt[15];
        snprintf(ip, sizeof(ip), "%u.%u.%u.%u", a, b, c, d);
    }
    lan_peers[nlan].ip = src;
    lan_peers[nlan].t = now;
    nlan++;
    /* policy routing is unavailable on this kernel (no ip rule/table in
     * busybox ip); a /32 host route always beats the /24 on eth0. */
    snprintf(cmd, sizeof(cmd),
             "%s route replace %s/32 dev %s 2>/dev/null", IP_CMD, ip, TUN_NAME);
    if (sh_rc(cmd) == 0) {
        printf("  [tun] learned peer %s -> replies go via %s\n", ip, TUN_NAME);
        fflush(stdout);
        return;
    }
    snprintf(cmd, sizeof(cmd),
             "%s route add %s/32 dev %s 2>/dev/null", IP_CMD, ip, TUN_NAME);
    sh(cmd);
    printf("  [tun] learned peer %s (add) -> replies go via %s\n", ip, TUN_NAME);
    fflush(stdout);
}

/* ---------------- tunnel stats / C3 state ---------------- */
static long ip_tx = 0, ip_rx = 0, ip_drop = 0;

/* ---------------- housekeeping pieces ---------------- */
static int udp_bind_ok(const char *ip)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in a;
    int ok;
    if (s < 0) return 0;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = 0;
    inet_pton(AF_INET, ip, &a.sin_addr);
    ok = bind(s, (struct sockaddr *)&a, sizeof(a)) == 0;
    close(s);
    return ok;
}

static int loopback_ok(void) { return udp_bind_ok("127.0.0.1"); }
static int addr_ok(void) { return udp_bind_ok(TUN_LOCAL_IP); }

/* default route lines: parse `ip route show`, take lines starting "default" */
static int default_has(const char *needle, int *ndefaults)
{
    FILE *f = popen(IP_CMD " route show 2>/dev/null", "r");
    char line[256];
    int n = 0, has = 0;
    if (!f) return 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "default", 7) == 0) {
            n++;
            if (strstr(line, needle)) has = 1;
        }
    }
    pclose(f);
    if (ndefaults) *ndefaults = n;
    return has;
}

static int tun_addr_text_has(const char *want)
{
    char cmd[128];
    char buf[512];
    FILE *f;
    int found = 0;
    snprintf(cmd, sizeof(cmd), IP_CMD " addr show dev %s 2>/dev/null", TUN_NAME);
    f = popen(cmd, "r");
    if (!f) return 0;
    while (fgets(buf, sizeof(buf), f)) {
        if (strstr(buf, want)) { found = 1; break; }
    }
    pclose(f);
    return found;
}

static void tun_configure(void)
{
    char cmd[160];
    int i, applied = 0;
    snprintf(cmd, sizeof(cmd), IP_CMD " link set %s up", TUN_NAME); sh(cmd);
    snprintf(cmd, sizeof(cmd), IP_CMD " link set %s mtu %d", TUN_NAME, TUN_MTU);
    sh(cmd);
    for (i = 0; i < 6; i++) {
        if (tun_addr_text_has(TUN_LOCAL_IP)) { applied = 1; break; }
        snprintf(cmd, sizeof(cmd), IP_CMD " addr add %s dev %s",
                 TUN_ADDR, TUN_NAME);
        sh(cmd);
        usleep(300000);
    }
    printf("  [tun] addr %s %s\n", TUN_ADDR, applied ? "OK" : "!! NOT applied");
    fflush(stdout);
    snprintf(cmd, sizeof(cmd), IP_CMD " route add 10.77.0.0/24 dev %s 2>/dev/null",
             TUN_NAME);
    sh(cmd);
    {
        int ndef = 0;
        default_has("", &ndef);
        if (ndef == 0) {
            snprintf(cmd, sizeof(cmd),
                     IP_CMD " route add default via %s dev %s 2>/dev/null",
                     TUN_PEER, TUN_NAME);
            sh(cmd);
            printf("  [tun] default route via %s\n", TUN_PEER);
            fflush(stdout);
        }
    }
}

static void clear_learned_routes(void)
{
    FILE *f = popen(IP_CMD " route show dev " TUN_NAME " 2>/dev/null", "r");
    char line[256];
    if (!f) return;
    while (fgets(line, sizeof(line), f)) {
        char *sp;
        char r[64];
        if (strlen(line) < 5) continue;
        strncpy(r, line, sizeof(r) - 1);
        r[sizeof(r) - 1] = 0;
        sp = strtok(r, " \t\n");
        if (sp && strlen(sp) > 3 && strcmp(sp + strlen(sp) - 3, "/32") == 0) {
            char cmd[128];
            snprintf(cmd, sizeof(cmd), IP_CMD " route del %s dev %s 2>/dev/null",
                     sp, TUN_NAME);
            sh(cmd);
        }
    }
    pclose(f);
}

static int tun_setup(void)
{
    tun_fd = tun_open();
    if (tun_fd < 0) return 0;
    tun_configure();
    clear_learned_routes();
    printf("  [tun] %s up: %s <-> %s, mtu %d\n",
           TUN_NAME, TUN_ADDR, TUN_PEER, TUN_MTU);
    fflush(stdout);
    return 1;
}

static int eth_carrier(void)
{
    FILE *f = fopen("/sys/class/net/eth0/carrier", "r");
    int c = -1;
    if (!f) return 0;
    if (fscanf(f, "%d", &c) != 1) c = -1;
    fclose(f);
    return c == 1;
}

static int nocarr = 0, nocarr_was = 0;

static void housekeeping(int *tun_retry)
{
    static int rt_tick = 0;
    rt_tick++;

    if (tun_fd < 0) {
        (*tun_retry)++;
        if ((*tun_retry) % 6 == 1) {
            if (!tun_setup())
                printf("  [tun] still no tun0; will keep retrying\n");
        }
        return;
    }

    if (!loopback_ok()) {
        sh(IP_CMD " link set lo up");
        sh(IP_CMD " addr add 127.0.0.1/8 dev lo 2>/dev/null");
        printf("  [net] !! 127.0.0.1 was missing on lo -- restored it\n");
        fflush(stdout);
    }

    {
        int carrier = eth_carrier();
        int need_check;
        if (!carrier) nocarr++; else nocarr = 0;
        need_check = (!carrier) || (rt_tick % 6 == 0);
        if (need_check) {
            int ndef = 0;
            int has_tun = default_has("dev " TUN_NAME, &ndef);
            int has_eth = default_has("dev eth0", &ndef);
            char cmd[160];
            if (ndef == 0) {
                snprintf(cmd, sizeof(cmd),
                         IP_CMD " route add default via %s dev %s 2>/dev/null",
                         TUN_PEER, TUN_NAME);
                sh(cmd);
                printf("  [tun] no default route -> installed via %s\n",
                       TUN_NAME);
                fflush(stdout);
            } else if (!carrier && !has_tun) {
                if (has_eth) sh(IP_CMD " route del default dev eth0 2>/dev/null");
                snprintf(cmd, sizeof(cmd),
                         IP_CMD " route add default via %s dev %s 2>/dev/null",
                         TUN_PEER, TUN_NAME);
                sh(cmd);
                printf("  [tun] cable out -> default route moved to %s\n",
                       TUN_NAME);
                fflush(stdout);
            } else if (carrier && nocarr_was) {
                if (has_tun) {
                    sh(IP_CMD " route del default via " TUN_PEER " dev "
                              TUN_NAME " 2>/dev/null");
                    printf("  [tun] cable back -> default route returned to "
                           "eth0\n");
                    fflush(stdout);
                }
            }
        }
        nocarr_was = nocarr > 0;
    }

    /* expire learned peer routes */
    {
        double now = now_s();
        int i;
        for (i = 0; i < nlan; i++) {
            if (now - lan_peers[i].t > PEER_IDLE) {
                uint32_t ip = lan_peers[i].ip;
                char cmd[128];
                snprintf(cmd, sizeof(cmd),
                         IP_CMD " route del %u.%u.%u.%u/32 dev %s 2>/dev/null",
                         ip & 0xFF, (ip >> 8) & 0xFF, (ip >> 16) & 0xFF,
                         (ip >> 24) & 0xFF, TUN_NAME);
                sh(cmd);
                lan_peers[i] = lan_peers[nlan - 1];
                nlan--;
                printf("  [tun] peer went quiet, withdrew its host route\n");
                fflush(stdout);
                i--; /* recheck this slot */
            }
        }
    }

    if (!addr_ok()) {
        printf("  [tun] !! %s vanished, reconfiguring\n", TUN_LOCAL_IP);
        fflush(stdout);
        tun_configure();
    }
}

/* udhcpc reaper: rkipc spawns `udhcpc -i tun0`; its helper flushes tun0's
 * addr and squats on port 554 (blocks RTSP). C scan is ~1 ms, cheap enough
 * to run from the pump's housekeeping every 20 s. */
static void reap_udhcpc_tun0(void)
{
    DIR *d = opendir("/proc");
    struct dirent *e;
    int nkilled = 0;
    if (!d) return;
    while ((e = readdir(d)) != NULL) {
        char path[64], cmd[256];
        FILE *f;
        size_t n;
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        snprintf(path, sizeof(path), "/proc/%s/cmdline", e->d_name);
        f = fopen(path, "rb");
        if (!f) continue;
        n = fread(cmd, 1, sizeof(cmd) - 1, f);
        fclose(f);
        cmd[n] = 0;
        /* cmdline is NUL-separated; make it greppable */
        {
            size_t i;
            for (i = 0; i < n; i++) if (cmd[i] == 0) cmd[i] = ' ';
        }
        if (strstr(cmd, "udhcpc") && strstr(cmd, "tun0")) {
            kill(atoi(e->d_name), SIGKILL);
            nkilled++;
        }
    }
    closedir(d);
    if (nkilled) {
        printf("  [tun] killed udhcpc on tun0 (%d) -- it holds port 554, "
               "which blocks RTSP\n", nkilled);
        fflush(stdout);
    }
}

/* ---------------- HELLO resync ---------------- */
static void hello_resync(void)
{
    int i, ok = 0;
    uint8_t rt;
    uint16_t ra;
    for (i = 0; i < 20; i++) {
        spi_seq = (spi_seq + 1) & 0xFFFFFFFF;   /* HELLO consumes a seq */
        spi_last_seq = spi_seq;
        int n = spi_exchange(T_HELLO, NULL, 0, spi_seq, &rt, &ra, NULL);
        if (n >= 0 && rt == T_HELLO) { ok = 1; break; }
        usleep(50000);
    }
    if (ok) {
        spi_seq = spi_last_seq;   /* continue from the HELLO's seq */
        outbox_clear();
        printf("  [pump] re-handshake done\n");
    } else {
        printf("  [pump] !! re-handshake failed, will retry\n");
    }
    fflush(stdout);
}

/* ---------------- pump ---------------- */
static double g_last_c3_up = 0;

static void pump(void)
{
    double last_housekeeping = 0, last_reap = now_s();
    double rate_t0 = now_s();
    int rate_n = 0, rate_bytes = 0, idle_streak = 0;
    long frames = 0, err = 0, retx_count = 0;
    uint16_t ack16 = 0;
    int have_ack = 0, ack16_seen = 0;
    double last_progress = now_s();
    int tun_retry = 0;
    static uint8_t pktbuf[FRAME];
    static uint8_t framebuf[MAX_PAYLOAD];
    double last_exchange = 0;

    outbox_clear();

    while (g_run) {
        double now;
        uint8_t send_type = 0;      /* 0 = nothing to send */
        int send_len = 0;
        uint32_t send_seq = 0;
        int is_retseq = 0;
        outbox_ent *retx_ent = NULL;
        uint8_t rt = 0;
        uint16_t ra = 0;
        int rcs_bad = 0;
        int rlen;
        int got_work = 0;
        int pktbuf_len = 0;

        /* 0) process acks only when the cumulative ack moved */
        if (have_ack && (!ack16_seen || ack16 != ack16_seen)) {
            int before = outbox_count(), i;
            for (i = 0; i < WINDOW; i++) {
                if (outbox[i].used && acked16(outbox[i].seq, ack16)) {
                    outbox[i].used = 0;
                }
            }
            if (outbox_count() < before) last_progress = now_s();
            ack16_seen = 1;
        }

        /* 1) stall guard */
        if (outbox_count() >= WINDOW && now_s() - last_progress > 3.0) {
            printf("  [pump] window stalled (%d unacked for 3s) -- "
                   "re-handshaking to resync\n", outbox_count());
            fflush(stdout);
            hello_resync();
            last_progress = now_s();
            err++;
        }

        /* 2) drain tun0 (max 4 per iteration) */
        {
            int k;
            for (k = 0; k < 4; k++) {
                int n = tun_read(pktbuf, FRAME);
                if (n <= 0) break;
                if (n > TUN_MTU) { ip_drop++; continue; }
                txq_push(pktbuf, n);
            }
        }

        now = now_s();
        if (now - last_housekeeping >= 5.0) {
            last_housekeeping = now;
            housekeeping(&tun_retry);
        }
        if (now - last_reap >= 20.0) {
            last_reap = now;
            reap_udhcpc_tun0();
        }

        /* 3) pick work: retx oldest overdue, else fresh frame, else probe */
        {
            outbox_ent *oldest = outbox_oldest();
            if (oldest && now - oldest->last_send >= RETRY_AFTER) {
                send_type = T_IP;
                send_len = oldest->plen;
                send_seq = oldest->seq;
                is_retseq = 1;
                retx_ent = oldest;
            } else if (!oldest || outbox_count() < WINDOW) {
                /* pack as many queued packets as fit in one frame */
                int off = 0;
                while (txq_cnt > 0) {
                    int need;
                    int pn = txq_len[txq_head];
                    need = 2 + pn;
                    if (off + need > MAX_PAYLOAD) break;
                    txq_pop(framebuf + off + 2);
                    framebuf[off] = pn & 0xFF;
                    framebuf[off + 1] = (pn >> 8) & 0xFF;
                    off += need;
                }
                if (off > 0) {
                    send_type = T_IP;
                    send_len = off;
                    pktbuf_len = 0;  /* payload lives in framebuf */
                }
            }
        }

        if (send_type == T_IP && is_retseq) {
            rlen = spi_exchange(T_IP, retx_ent->payload, send_len, send_seq,
                                &rt, &ra, &rcs_bad);
            retx_ent->last_send = now_s();
            retx_count++;
        } else if (send_type == T_IP) {
            spi_seq = (spi_seq + 1) & 0xFFFFFFFF;
            send_seq = spi_seq;
            spi_last_seq = send_seq;
            rlen = spi_exchange(T_IP, framebuf, send_len, send_seq,
                                &rt, &ra, &rcs_bad);
            outbox_add(send_seq, framebuf, send_len, now_s());
            ip_tx++;
            idle_streak = 0;
        } else {
            /* idle probe: MUST NOT consume a seq (the C5 only advances its
             * cumulative ack on T_IP; probes would widen the gap) */
            rlen = spi_exchange(T_NODATA, NULL, 0, spi_seq, &rt, &ra,
                                &rcs_bad);
            idle_streak++;
        }

        if (rlen < 0) {
            err++;
        } else {
            have_ack = 1;
            ack16 = ra;
            if (rt == T_STAT && rlen > 0) {
                /* C3 status: persist verbatim for the web page; up_ms going
                 * backwards means the C5 rebooted -> re-handshake */
                got_work = 1;
                {
                    FILE *f = fopen(C3_STATE_FILE, "w");
                    if (f) { fwrite(rxbuf + HDR, 1, rlen, f); fclose(f); }
                }
                {
                    char *p = memmem(rxbuf + HDR, rlen, "\"up_ms\":", 8);
                    double up = p ? atof(p + 8) : 0;
                    if (up > 0 && g_last_c3_up > 0 && up + 60000 < g_last_c3_up) {
                        printf("  [pump] C5 rebooted (up_ms went backwards), "
                               "re-handshaking ...\n");
                        fflush(stdout);
                        hello_resync();
                    }
                    if (up > 0) g_last_c3_up = up;
                }
            } else if (rt == T_IP && rlen > 0) {
                got_work = 1;
                {
                    uint16_t off = 0;
                    int first = 1;
                    while (off + 2 <= rlen) {
                        uint16_t plen = (uint16_t)(rxbuf[HDR + off] |
                                            (rxbuf[HDR + off + 1] << 8));
                        off += 2;
                        if (plen < 20 || (uint32_t)off + plen > (uint32_t)rlen) {
                            ip_drop++;
                            break;
                        }
                        if (first) {
                            learn_peer(rxbuf + HDR + off);
                            first = 0;
                        }
                        if (tun_fd >= 0 && tun_write(rxbuf + HDR + off, plen) > 0)
                            ip_rx++;
                        else
                            ip_drop++;
                        off += plen;
                    }
                }
            }
        }

        /* Pace to ~300 exchanges/s max.
         *
         * The C pump is FASTER than the C5 slave can re-arm its DMA slots:
         * at full tilt (340-360 fps) the slave is occasionally unarmed when
         * the master clocks a frame -> bad magic -> retx (~1% of frames).
         * Each retx is a 3ms stall + an arrival-latency spike; the spikes
         * inflate Chrome's jitter buffer (measured 105 ms) and that -- not
         * the wire -- is the dominant video latency. spinet.py never saw
         * this because its interpreter overhead accidentally paced it to
         * 200-290 fps with fails ~= 0.
         * 3.2ms floor ~= 300 fps cap; wire throughput stays ~7.5 Mbps,
         * far above the 1.9 Mbps video stream, and the sleep is CPU-free. */
        {
            double since = now_s() - last_exchange;
            if (since >= 0 && since < 0.0032)
                usleep((useconds_t)((0.0032 - since) * 1e6));
        }
        last_exchange = now_s();
        frames++;
        rate_n++;
        if (send_type == T_IP && !is_retseq) rate_bytes += send_len;
        if (rate_n % 200 == 0) {
            double dt = now_s() - rate_t0;
            if (dt > 0) {
                double up = -1;
                FILE *f = fopen("/proc/uptime", "r");
                if (f) { double a, b; if (fscanf(f, "%lf %lf", &a, &b) == 2) up = a; fclose(f); }
                printf("  [pump] t=%6.1fs  %.0f frames/s, %.1f KB/s up "
                       "(fails=%ld, retx=%ld, ip tx=%ld rx=%ld drop=%ld)\n",
                       up, rate_n / dt, rate_bytes / dt / 1024.0,
                       err, retx_count, ip_tx, ip_rx, ip_drop);
                fflush(stdout);
            }
            rate_t0 = now_s();
            rate_n = 0;
            rate_bytes = 0;
        }

        /* idle backoff (2026-10-07): escalate 1 -> 4 -> 8 ms after sustained
         * true idle; ANY real work resets. Worst-case added latency <= 8 ms. */
        if (send_type == 0 && !got_work) {
            double d = 0.001;
            if (idle_streak >= 30) d = 0.008;
            else if (idle_streak >= 10) d = 0.004;
            usleep((useconds_t)(d * 1e6));
        } else {
            idle_streak = 0;
        }

        (void)pktbuf_len;
    }
}

/* ---------------- main ---------------- */
int main(void)
{
    uint32_t speed;
    char dev[] = "/dev/spidev0.0";
    double t0;
    int hello_ok = 0, i;

    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    signal(SIGPIPE, SIG_IGN);
    setvbuf(stdout, NULL, _IOLBF, 0);

    printf("========================================================\n");
    printf(" SPI tunnel over TUN (C pump, no interpreter)\n");
    printf("========================================================\n");

    speed = spi_speed();
    if (spi_open(dev, speed) < 0) {
        printf("  !! cannot open %s: %s\n", dev, strerror(errno));
        return 1;
    }
    printf("  SPI %s @ %u Hz\n", dev, speed);

    /* wait for the C5, up to 120 s (it may still be associating WiFi) */
    printf("  waiting for the C5 ...\n");
    t0 = now_s();
    {
        double last_note = 0;
        while (now_s() - t0 < 120.0 && g_run) {
            uint8_t rt = 0;
            uint16_t ra = 0;
            int rlen;
            spi_seq = (spi_seq + 1) & 0xFFFFFFFF;
            spi_last_seq = spi_seq;
            rlen = spi_exchange(T_HELLO, NULL, 0, spi_seq, &rt, &ra, NULL);
            if (rlen >= 0 && rt == T_HELLO) { hello_ok = 1; break; }
            {
                double w = now_s() - t0;
                if (w - last_note >= 5.0) {
                    last_note = w;
                    printf("  still no C5 (%.0fs) -- keep waiting\n", w);
                    fflush(stdout);
                }
            }
            usleep(50000);
        }
    }
    if (!hello_ok) {
        printf("  !! no C5 handshake in 120 s; exiting (watchdog will retry)\n");
        return 1;
    }
    printf("  C5 handshake OK\n");
    /* the slave resynced its ACK window to the HELLO's seq: continue from it */
    spi_seq = spi_last_seq;

    /* pump runs in THIS thread; tun0 may not exist yet -- housekeeping
     * retries every ~30 s without ever taking the SPI link down */
    if (tun_setup()) {
        printf("\n  self-test: board -> C5 spi0 (%s), TTL=1\n", TUN_PEER);
        {
            int rc = 1, a;
            char out[512] = "";
            FILE *f;
            f = popen(IP_CMD " route get " TUN_PEER " 2>/dev/null", "r");
            if (f) {
                char line[256];
                while (fgets(line, sizeof(line), f)) {
                    strncat(out, line, sizeof(out) - strlen(out) - 1);
                }
                pclose(f);
                printf("  route: %s", out);
            }
            for (a = 0; a < 3 && rc != 0; a++) {
                rc = sh_rc("ping -c 2 -W 2 -t 1 " TUN_PEER " >/dev/null 2>&1");
                if (rc != 0 && a < 2) sleep(2);
            }
            printf("  %s\n", rc == 0 ? "TUN link OK -- the board has a real IP"
                                     : "!! TUN link down (pump keeps running)");
        }
    } else {
        printf("  !! tun0 unavailable; SPI link keeps running, retries ~30s\n");
    }

    fflush(stdout);
    pump();

    close(spi_fd);
    if (tun_fd >= 0) close(tun_fd);
    return 0;
}

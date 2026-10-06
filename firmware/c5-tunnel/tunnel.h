/*
 * tunnel.h — SPI 隧道协议 (Luckfox master <-> ESP32-C3 slave)
 *
 * 应用层转发协议: 板子上的本地代理收到连接后, 通过 SPI 请求 C3
 * 用自己的 WiFi socket 去连目标, 然后双向搬字节。
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#include "lwip/err.h"

struct pbuf;   /* 只在 tunnel_ip_tx 的签名里用到指针, 不必拉进整个 lwIP */

#define TUN_FRAME_SIZE   4096
#define TUN_HDR_SIZE     16
#define TUN_MAX_PAYLOAD  (TUN_FRAME_SIZE - TUN_HDR_SIZE)   /* 4080 */

/* 魔数 'L','F','T','2' */
#define TUN_MAGIC        0x3254464CUL

/* 帧类型 (SPI 层) */
#define TUN_T_HELLO      0x01
#define TUN_T_APP        0x02   /* 载荷是应用层消息 */
#define TUN_T_NODATA     0x03
#define TUN_T_STAT       0x04
#define TUN_T_IP         0x05   /* 载荷是一串裸 IP 报文 (板子 tun0 <-> C3 spi0)
                                 *
                                 * 布局: 重复的 [2B 小端长度][报文]。
                                 *
                                 * 一帧可以装多个包 —— 每个包单独发一帧会浪费
                                 * 4080 字节载荷里的一大半, 而这条链路是
                                 * stop-and-wait, 吞吐 = 每往返字节数, 所以
                                 * 一帧多装几个包是收益最大的改动。
                                 * 三个 1350 字节的包 (3x1352=4056) 正好装下。 */

/* 应用层消息 (TUN_T_APP 的载荷里) */
#define APP_OPEN         0x10   /* master->slave: 请连目标 (出站代理) */
#define APP_DATA         0x11   /* 双向: 连接上的数据 */
#define APP_CLOSE        0x12   /* 双向: 关闭连接 */
#define APP_OPENOK       0x13   /* slave->master: 连上了 */
#define APP_OPENFAIL     0x14   /* slave->master: 连不上 */

/* 应用层头: [1B msg][4B conn_id] = 5 字节 */
#define APP_HDR_SIZE     5

/*
 * 线上帧头 (小端):
 *   off  0  u32 magic
 *   off  4  u8  type
 *   off  5  u8  flags
 *   off  6  u16 reserved
 *   off  8  u32 seq
 *   off 12  u16 len
 *   off 14  u16 csum
 */
typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  type;
    uint8_t  flags;
    uint16_t reserved;
    uint32_t seq;
    uint16_t len;
    uint16_t csum;
} tun_hdr_t;

_Static_assert(sizeof(tun_hdr_t) == TUN_HDR_SIZE, "tun_hdr_t must be 16 bytes");

/*
 * APP_OPEN 的载荷 (跟在 5 字节 app 头之后):
 *   [2B hostlen][hostlen B host][2B port]     全部小端
 * 注意 port 在 host 之后, 不是紧跟 hostlen。
 */

static inline uint16_t tun_csum16(const uint8_t *p, size_t n)
{
    /* MUST match csum16() in car/spinet.py, which is zlib.adler32(b) & 0xFFFF.
     *
     * adler32 = (B << 16) | A, and A = (1 + sum(bytes)) % 65521 -- so the low
     * 16 bits are exactly this. The master used to evaluate `sum(b) & 0xFFFF`
     * in Python, which walks the payload one byte at a time in the interpreter:
     * measured 0.550 ms for a 4056-byte frame, and every exchange computes it
     * twice (once on the payload sent, once on the payload received). That was
     * roughly half of the 2.1 ms an exchange takes.
     *
     * In C this loop is negligible, so switching the master to a C-level
     * checksum and matching it here removes the bottleneck on both sides. */
    uint32_t s = 1;
    for (size_t i = 0; i < n; i++) s += p[i];
    return (uint16_t)(s % 65521u);
}

esp_err_t tunnel_init(void);
void tunnel_task(void *arg);
void tunnel_stats(uint32_t *up, uint32_t *down, uint32_t *drop, uint32_t *err);

/* ---- TUN 数据通路 (由 spinet.c 的 linkoutput 调用) ----
 *
 * 把 lwIP 要发的 IP 报文排进高优先队列, 由 tunnel_task 打包成 TUN_T_IP 帧
 * 发给板子。在 tcpip 线程里调用, 所以只做一次 memcpy + 非阻塞入队。 */
err_t tunnel_ip_tx(struct pbuf *p);

/* IP 帧的收发计数, 供状态行使用 */
void tunnel_ip_stats(uint32_t *tx, uint32_t *drop);

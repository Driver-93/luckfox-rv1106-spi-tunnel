/*
 * spinet.h — 通过 SPI 隧道承载的 lwIP 网卡 (C3 侧)
 *
 * 目的
 * ----
 * 板子的内核本来没有 CONFIG_TUN, 所以它无法拥有一个"真正的"网络接口,
 * 只能在用户态把每条 TCP 连接重建一遍 —— 那就是槽位表/反向代理/无重传/
 * 无 UDP 的根源。tun.ko 补上之后, 板子有了 tun0, 于是 C3 这边也只需要
 * 一个普通的点对点 IP 网卡: 两边交换裸 IP 报文, 上层完全是标准 TCP/IP。
 *
 *   [浏览器] --WiFi--> C3(192.168.3.69) --NAPT--> spi0(10.77.0.1)
 *                                                    |  SPI 隧道 (裸 IP)
 *                                                    v
 *                              [板子 tun0 10.77.0.2] -- 标准 socket
 *
 * 为什么 C3 的 spi0 要开 NAPT, 而 WiFi 网卡不开
 * --------------------------------------------
 * ESP-IDF 的 NAPT 里 `netif->napt` 标记的是 **LAN 侧** 网卡, 不是 WAN 侧。
 * 看 ip4.c 的两处钩子:
 *
 *   转发时:  if (!outp->napt)  ip_napt_forward(p, iphdr, inp, outp);
 *   收包时:  if (!inp->napt && dest == netif_ip4_addr(inp)) ip_napt_recv(...);
 *
 * ip_napt_forward 内部第一行是 `if (!inp->napt) return ERR_OK;` —— 它要求
 * **入接口** 带 napt 标志。板子发出的包从 spi0 进来, 所以 spi0 必须是
 * napt=1; 出接口是 WiFi, 必须是 napt=0。反过来配的话源地址不会被改写,
 * 局域网根本没有回 10.77.0.2 的路由。
 *
 * 入站端口映射 (浏览器 -> 板子)
 * ----------------------------
 * 来自 WiFi 的包以 192.168.3.69:port 为目的, 在 ip4_input 里命中
 * `!inp->napt && dest==本机IP`, 进入 ip_napt_recv, 查 ip_portmap 表把目的
 * 改写成 10.77.0.2:port, 再路由到 spi0。所以 browser -> 板子 是通的,
 * 不需要任何用户态反向代理。
 *
 * 注意 ip_portmap_add 的 maddr 参数 = 本节点对外的地址 (192.168.3.69)。
 * 它不是可有可无的: 板子的回程包 (src=10.77.0.2:80) 会在 ip_napt_forward
 * 里被 ip_portmap_find_dest 命中, 然后把源地址改写成 maddr。传 0 的话
 * 源地址会变成 0.0.0.0, 连接直接坏掉。
 */
#pragma once
#include <stdint.h>
#include "esp_err.h"

/* 点对点地址。板子 tun0 = 10.77.0.2, C3 spi0 = 10.77.0.1。 */
#define SPINET_C3_IP     10, 77, 0, 1
#define SPINET_BOARD_IP  10, 77, 0, 2

/* MTU for the point-to-point link. MUST match TUN_MTU in car/spinet.py.
 *
 * 1350 is chosen so that exactly three packets fit in one SPI frame:
 *     3 x (1350 + 20 IP hdr) + 3 x 2 length bytes = 4116  -- too big
 *     3 x 1350 + 3 x 2                             = 4056  <= 4080 payload
 * Since the link is stop-and-wait, throughput is bytes-per-round-trip, so
 * packing three packets instead of one is worth ~2.8x.
 *
 * (1400 would only fit two: 2 x 1420 = 2840.) */
#define SPINET_MTU       1350

/* 建 spi0 网卡并打开 NAPT + 端口映射。
 * 必须在 WiFi 拿到地址之后、tunnel_task 起来之前调用。
 * 内部通过 tcpip_callback 在 lwIP 线程里完成, 失败返回非 ESP_OK。 */
esp_err_t spinet_start(void);

/* 从隧道收到一个裸 IP 报文时调用 (由 tunnel.c 派发)。
 * 内部申请 pbuf 并交给 tcpip_input —— 会转移 pbuf 所有权。 */
void spinet_input(const uint8_t *data, uint16_t len);

/* 统计, 供状态行输出 */
void spinet_stats(uint32_t *tx, uint32_t *rx, uint32_t *drop, uint32_t *maps);

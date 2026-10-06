/*
 * spinet.c — 见 spinet.h 顶部的架构说明。
 */
#include <string.h>
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "lwip/netif.h"
#include "lwip/tcpip.h"
#include "lwip/pbuf.h"
#include "lwip/ip4_addr.h"
#include "lwip/prot/ip.h"
#include "lwip/lwip_napt.h"
#include "spinet.h"
#include "tunnel.h"

static const char *TAG = "spinet";

static struct netif      s_netif;
static SemaphoreHandle_t s_done;
static volatile int      s_up   = 0;
static volatile int      s_ok   = 0;

static volatile uint32_t s_tx, s_rx, s_drop, s_maps;

/* 需要从局域网访问板子的端口。
 *   80   web 控制页 / 图传页面
 *   22   SSH —— 没有网线时这是唯一的登录途径
 *   8889 MediaMTX WHEP 信令 (HTTP over TCP)
 *   8189 MediaMTX WebRTC —— 同一个端口既是 ICE-TCP 也是媒体 UDP
 * 8888 MediaMTX HLS —— TUN 架构下 UDP 可用, WebRTC 为主路径;
 *        HLS 保留为浏览器兼容/后备, 所以加回映射。 */
static const uint16_t FWD_TCP[] = { 80, 22, 8889, 8189, 8888 };
static const uint16_t FWD_UDP[] = { 8189 };

/* ---------------- lwIP 回调 (都在 tcpip 线程上下文) ---------------- */

/* lwIP 把要发的 IP 报文交给我们。tcpip 线程里调用, 不能阻塞。 */
static err_t spinet_link_output(struct netif *netif, struct pbuf *p)
{
    (void)netif;
    if (p->tot_len == 0 || p->tot_len > SPINET_MTU) {
        s_drop++;
        return ERR_MEM;
    }
    err_t e = tunnel_ip_tx(p);
    if (e == ERR_OK) s_tx++;
    else             s_drop++;
    return e;
}

/* 纯点对点链路: 没有以太网头, 没有 ARP, 直接把 IP 报文交给链路层。 */
static err_t spinet_netif_output(struct netif *netif, struct pbuf *p,
                                 const ip4_addr_t *ipaddr)
{
    (void)ipaddr;
    return spinet_link_output(netif, p);
}

static err_t spinet_netif_init(struct netif *netif)
{
    netif->name[0] = 's';
    netif->name[1] = 'p';

    /* 只有 output / linkoutput, 没有 NETIF_FLAG_ETHARP —— 这一层没有 MAC。
     *
     * 也**绝不能**加 NETIF_FLAG_BROADCAST。点对点链路没有广播地址, 而在 /24
     * 上打开这个标志会引发一个很难查的后果:
     *
     *   ip4_addr_isbroadcast(10.77.0.2, spi0)
     *     = ip4_addr_netcmp(10.77.0.2, 10.77.0.1, 255.255.255.0)   // 同网段
     *       && !ip4_addr_cmp(10.77.0.2, 10.77.0.1)                 // 主机位不同
     *     = TRUE
     *
     * 于是 ip4_input 找目的网卡的循环会把改写后的入站包 (目的 10.77.0.2)
     * 判成"C3 自己的广播", netif != NULL, 于是**不进转发分支**, 而是本地投递 ——
     * C3 上根本没有 socket 监听 10.77.0.2, 包就这么静默消失了。
     *
     * 症状: 板子 -> 外网 正常 (8.8.8.8 不匹配任何网卡, 老老实实走转发),
     *       浏览器 -> 板子 卡死 (被误判为广播), 而 C3 的转发计数一动不动。
     */
    netif->output     = spinet_netif_output;
    netif->linkoutput = spinet_link_output;
    netif->mtu        = SPINET_MTU;
    netif->flags      = NETIF_FLAG_LINK_UP;
    netif->hwaddr_len = 0;
    netif->napt       = 0;      /* 稍后由 ip_napt_enable_netif 置 1 */
    return ERR_OK;
}

/* ---------------- 建网卡 + NAPT + 端口映射 ---------------- */

typedef struct {
    uint32_t external_ip;   /* 网络字节序, 即 192.168.3.69 */
} add_arg_t;

static void spinet_add_cb(void *arg)
{
    add_arg_t *a = (add_arg_t *)arg;

    ip4_addr_t ip, nm, gw;
    /* IP4_ADDR is a 5-argument macro, so a macro list (SPINET_C3_IP) cannot be
     * passed through it -- spell the octets out. spinet.h keeps the constants
     * in sync with the board's tun0 address. */
    IP4_ADDR(&ip, 10, 77, 0, 1);
    IP4_ADDR(&nm, 255, 255, 255, 0);
    IP4_ADDR(&gw, 10, 77, 0, 1);

    /* tcpip_input: 收到的报文会排进 tcpip 线程, 因此从任意任务调用都安全。 */
    struct netif *n = netif_add(&s_netif, &ip, &nm, &gw, NULL,
                                spinet_netif_init, tcpip_input);
    if (!n) {
        ESP_LOGE(TAG, "netif_add 失败");
        goto out;
    }

    netif_set_up(n);
    netif_set_link_up(n);

    /* napt=1 打在 LAN 侧 (spi0)。WiFi 网卡保持 napt=0, 否则 ip4_input 里
     * 的端口映射钩子不会触发, 入站连接进不来。见 spinet.h 的推导。 */
    if (ip_napt_enable_netif(n, 1) == 0) {
        ESP_LOGE(TAG, "ip_napt_enable_netif 失败");
        goto out;
    }

    /* 板子 tun0 的地址, 即端口映射的**目标**。
     *
     * 这里曾经写成 `ip4_addr_get_u32(&ip)`, 而 `ip` 是上面刚设好的 10.77.0.1
     * —— 也就是 C3 自己。于是 ip_napt_recv 把每个入站连接的目的地址改写成
     * C3 自己的地址, ip4_input_accept() 一看"目的 == 本机地址"就当作本地投递,
     * 永远不进转发分支, 而 C3 上根本没有 socket 监听 10.77.0.1:80。
     *
     * 症状极具误导性: SYN 被 portmap 截住 (C3 自己的反向代理收不到了),
     * 浏览器就是一直超时, 而两个方向的数据面其实都是好的。 */
    ip4_addr_t board_ip;
    IP4_ADDR(&board_ip, 10, 77, 0, 2);
    uint32_t board    = ip4_addr_get_u32(&board_ip);   /* 10.77.0.2 */
    uint32_t external = a->external_ip;                /* 192.168.3.69 */

    s_maps = 0;
    for (unsigned i = 0; i < sizeof(FWD_TCP) / sizeof(FWD_TCP[0]); i++) {
        if (ip_portmap_add(IP_PROTO_TCP, external, FWD_TCP[i], board, FWD_TCP[i]))
            s_maps++;
        else
            ESP_LOGW(TAG, "TCP %u 端口映射失败 (表满?)", FWD_TCP[i]);
    }
    for (unsigned i = 0; i < sizeof(FWD_UDP) / sizeof(FWD_UDP[0]); i++) {
        if (ip_portmap_add(IP_PROTO_UDP, external, FWD_UDP[i], board, FWD_UDP[i]))
            s_maps++;
        else
            ESP_LOGW(TAG, "UDP %u 端口映射失败 (表满?)", FWD_UDP[i]);
    }

    s_up = 1;
    s_ok = 1;
    ESP_LOGI(TAG, "spi0 up: 10.77.0.1/24 <-> 板子 10.77.0.2, mtu %d, %lu 条端口映射 -> " IPSTR,
             SPINET_MTU, (unsigned long)s_maps, IP2STR(&ip));

out:
    xSemaphoreGive(s_done);
}

esp_err_t spinet_start(void)
{
    s_done = xSemaphoreCreateBinary();
    if (!s_done) return ESP_ERR_NO_MEM;

    /* 端口映射里 maddr 必须是对外地址。传 0 会让板子的回程包源地址变成
     * 0.0.0.0 —— 见 spinet.h 的说明。 */
    uint32_t external = 0;
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (sta) {
        esp_netif_ip_info_t info;
        if (esp_netif_get_ip_info(sta, &info) == ESP_OK && info.ip.addr != 0)
            external = info.ip.addr;
    }
    if (external == 0) {
        IP4_ADDR((ip4_addr_t *)&external, 192, 168, 3, 69);
        ESP_LOGW(TAG, "取不到 STA 地址, 端口映射回退到 192.168.3.69");
    }

    add_arg_t a = { .external_ip = external };
    if (tcpip_callback(spinet_add_cb, &a) != ERR_OK) return ESP_FAIL;

    /* 回调里只做内存分配和小表填写, 1 秒足够; 超时也不致命。 */
    if (xSemaphoreTake(s_done, pdMS_TO_TICKS(1000)) != pdTRUE) {
        ESP_LOGW(TAG, "等待 tcpip 回调超时");
        return ESP_ERR_TIMEOUT;
    }
    return s_ok ? ESP_OK : ESP_FAIL;
}

/* ---------------- 收包 ---------------- */

void spinet_input(const uint8_t *data, uint16_t len)
{
    if (!s_up || len == 0 || len > SPINET_MTU) {
        if (len) s_drop++;
        return;
    }

    /* PBUF_LINK, not PBUF_RAW.
     *
     * pbuf_alloc() turns the layer argument straight into the headroom offset
     * in front of the payload, and PBUF_RAW is 0 -- no headroom at all. That
     * only matters once a packet is FORWARDED: ethernet_output() starts with
     * pbuf_add_header(p, SIZEOF_ETH_HDR) and bails out with ERR_BUF if the
     * front of the pbuf has no room for the 14-byte Ethernet header.
     *
     * The symptom is nasty because it is invisible: the C3 happily receives
     * every packet from the board (ip rx climbing), answers pings addressed to
     * its OWN IP (delivered locally, no Ethernet header needed), and silently
     * drops everything it is asked to forward. Measured exactly that --
     * ip rx=400, ip tx=6, and every ping forced through tun0 timing out.
     *
     * PBUF_LINK reserves PBUF_LINK_ENCAPSULATION_HLEN + PBUF_LINK_HLEN bytes,
     * which is what the forwarding path needs. */
    struct pbuf *p = pbuf_alloc(PBUF_LINK, len, PBUF_POOL);
    if (!p) { s_drop++; return; }

    if (pbuf_take(p, data, len) != ERR_OK) {
        pbuf_free(p);
        s_drop++;
        return;
    }

    /* tcpip_input 成功时接管 pbuf; 失败时返回非 ERR_OK, 由我们释放。 */
    if (s_netif.input(p, &s_netif) != ERR_OK) {
        pbuf_free(p);
        s_drop++;
        return;
    }
    s_rx++;
}

void spinet_stats(uint32_t *tx, uint32_t *rx, uint32_t *drop, uint32_t *maps)
{
    if (tx)   *tx   = s_tx;
    if (rx)   *rx   = s_rx;
    if (drop) *drop = s_drop;
    if (maps) *maps = s_maps;
}

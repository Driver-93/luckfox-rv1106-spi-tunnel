/*
 * tunnel.c — ESP32-C3 侧: SPI <-> WiFi socket 转发
 *
 * 板子上的代理收到本地连接后, 发 APP_OPEN(host,port) 过来。
 * 本模块用 lwIP socket 去连目标, 之后双向搬字节。
 *
 * 并发模型:
 *   - 1 个 SPI 任务: 收 master 的帧, 派发; 回帧时带上待下行数据
 *   - 每个连接 1 个 worker 任务: recv 目标数据 -> 塞进下行队列
 *
 * 连接表用固定数组, C3 只有 149KB 堆, 不能动态无限开。
 */
#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/pbuf.h"
#include "tunnel.h"
#include "spi_slave.h"
#include "spinet.h"
#include "netlog.h"

static const char *TAG = "tunnel";

#define MAX_CONN        8
#define CONN_RXBUF      4096
#define DOWNQ_LEN       32
#define WORKER_STACK    5120

/* ---- IP 报文队列 (板子 tun0 <-> C3 spi0) ----
 *
 * 和 DOWNQ 分开, 而且在发送时优先取。原因: 走 DOWNQ 的是反向代理的载荷,
 * 一条视频分段可能有几十 KB, 把队列填满会让 IP 报文排在后面 —— 表现为
 * 控制指令的延迟抖动。IP 报文只有 ~1.4KB, 单独一条队列既保证优先,
 * 也避免被大块数据堵住。
 *
 * 深度 16: 每个待发报文约 1.4KB 堆, 最坏 ~23KB。C3 只有 149KB 堆,
 * 配合 main.c 里的分级保护是安全的。满了就丢 —— TCP 会重传, UDP 媒体
 * 丢一帧无感, 都比卡住整条链路好。 */
#define IPQ_LEN         16
typedef struct { uint16_t len; uint8_t *data; } ip_item_t;
static QueueHandle_t s_ipq;
static volatile uint32_t s_ip_tx, s_ip_drop, s_ip_rx;

/* ---- 下行队列: 要塞回 master 的数据 ---- */
typedef struct { uint16_t len; uint8_t *data; } down_item_t;
static QueueHandle_t s_downq;

/* ---- 连接表 ---- */
typedef struct {
    int      used;
    int      sock;
    uint32_t id;
    TaskHandle_t worker;
    volatile int closing;
} conn_t;

static conn_t s_conns[MAX_CONN];
static SemaphoreHandle_t s_conn_lock;

static volatile uint32_t s_up, s_down, s_drop, s_err;

/* ---- 可靠传输: 顺序确认 + 去重 ----
 *
 * 实测这套机制从未真正触发: 板上连续跑了整个会话, retx 恒为 0,
 * fails 最大 1 (启动早期从机还没武装好那一次)。
 *
 * 但**暂时不能删**: 板上运行的是一版带滑动窗口和重传的主机
 * (spinet.py), 它依赖这里回填的累积确认来推进发送窗口。删掉它
 * 主机侧就会卡住。正确顺序是先简化主机, 再删这里。
 *
 * 窗口掩码: s_ack_seq 是"连续收到的最大序号", 掩码第 i 位表示
 * seq = s_ack_seq + 1 + i 已收到。前进时整块右移, 没有按位平移,
 * 因此不会出现老版本那种"没收到过的帧被当成重复"的错位
 * (老版本按字节平移按位计算的位移量, 前进量不是 8 的倍数时就错位)。
 */
#define ACK_WIN  64
static uint32_t s_ack_seq = 0;          /* 连续确认到的 seq */
static uint64_t s_ack_mask = 0;         /* bit i => ack_seq+1+i 已收到 */

/* 返回 1 表示这是新帧 (应该处理), 0 表示重复或超出窗口 (丢弃)。 */
static int ack_accept(uint32_t seq)
{
    if (seq == 0) return 1;                 /* 无序号帧 (HELLO 等) 不过滤 */

    int32_t d = (int32_t)(seq - s_ack_seq);

    if (d <= 0) return 0;                   /* 老的 -> 重复 */

    if (d > ACK_WIN) {
        /* 超出窗口。丢弃而不是重同步: 真正的滑动窗口里丢一帧由主机重传
         * 补上, 重同步会把丢失的那帧直接跳过。 */
        return 0;
    }

    uint64_t bit = 1ULL << (d - 1);
    if (s_ack_mask & bit) return 0;         /* 已经收过了 */
    s_ack_mask |= bit;

    /* 推进连续确认指针, 并整块右移掩码。 */
    while ((s_ack_mask & 1ULL) != 0) {
        s_ack_seq++;
        s_ack_mask >>= 1;
    }
    return 1;
}

/* 前置声明 */
static void do_open_blocking(uint32_t cid, const char *host, uint16_t port);
static void do_open_async(uint32_t cid, const uint8_t *pl, uint16_t len);
static void push_down(uint8_t msg, uint32_t cid, const uint8_t *data, uint16_t len);

/* ---------------- 下行入队 ---------------- */

static void push_down(uint8_t msg, uint32_t cid, const uint8_t *data, uint16_t len)
{
    if (len > TUN_MAX_PAYLOAD - APP_HDR_SIZE) { s_drop++; return; }

    /* Drop-count visibility: silent drops here truncate every response and
     * are invisible from the master's side. */
    if (uxQueueSpacesAvailable(s_downq) == 0) {
        s_drop++;
        if ((s_drop & 0x1F) == 1)
            ESP_LOGW(TAG, "downq 满, 丢弃 msg=0x%02x cid=%lu len=%u",
                     msg, (unsigned long)cid, len);
        return;
    }

    uint16_t total = APP_HDR_SIZE + len;
    uint8_t *buf = malloc(total);
    if (!buf) { s_drop++; return; }

    buf[0] = msg;
    buf[1] = cid & 0xFF;
    buf[2] = (cid >> 8) & 0xFF;
    buf[3] = (cid >> 16) & 0xFF;
    buf[4] = (cid >> 24) & 0xFF;
    if (len) memcpy(buf + APP_HDR_SIZE, data, len);

    down_item_t it = { .len = total, .data = buf };
    if (xQueueSend(s_downq, &it, 0) != pdTRUE) {
        free(buf);
        s_drop++;
    } else {
        s_down++;
    }
}

/* ---------------- IP 报文通路 (板子 tun0 <-> C3 spi0) ---------------- */

/* spinet.c 的 linkoutput 调用: lwIP 要发一个 IP 报文。
 * 运行在 tcpip 线程 -> 只做 memcpy + 非阻塞入队, 绝不阻塞网络栈。 */
err_t tunnel_ip_tx(struct pbuf *p)
{
    uint16_t len = p->tot_len;
    if (len == 0 || len > SPINET_MTU) { s_ip_drop++; return ERR_MEM; }

    /* 先查空间再 malloc, 避免队列满时白白分配又释放造成堆碎片。 */
    if (uxQueueSpacesAvailable(s_ipq) == 0) { s_ip_drop++; return ERR_MEM; }

    uint8_t *buf = malloc(len);
    if (!buf) { s_ip_drop++; return ERR_MEM; }

    /* pbuf 可能是链式的, 必须按偏移线性化。 */
    if (pbuf_copy_partial(p, buf, len, 0) != len) {
        free(buf);
        s_ip_drop++;
        return ERR_MEM;
    }

    ip_item_t it = { .len = len, .data = buf };
    if (xQueueSend(s_ipq, &it, 0) != pdTRUE) {
        free(buf);
        s_ip_drop++;
        return ERR_MEM;
    }
    s_ip_tx++;
    return ERR_OK;
}

void tunnel_ip_stats(uint32_t *tx, uint32_t *drop)
{
    if (tx)   *tx   = s_ip_tx;
    if (drop) *drop = s_ip_drop;
}

/* ---------------- 连接管理 ---------------- */

static conn_t *conn_find(uint32_t cid)
{
    for (int i = 0; i < MAX_CONN; i++)
        if (s_conns[i].used && s_conns[i].id == cid) return &s_conns[i];
    return NULL;
}

/* 读目标 socket -> 入下行队列 */
static void conn_worker(void *arg)
{
    conn_t *c = (conn_t *)arg;
    /* 动态分配, 避免占 worker 栈 (栈太小容易溢出) */
    uint8_t *buf = malloc(1024);
    if (!buf) {
        push_down(APP_CLOSE, c->id, NULL, 0);
        goto cleanup;
    }

    for (;;) {
        if (c->closing) break;
        int n = recv(c->sock, buf, 1024, 0);
        if (n > 0) {
            push_down(APP_DATA, c->id, buf, (uint16_t)n);
        } else if (n == 0) {
            push_down(APP_CLOSE, c->id, NULL, 0);
            break;
        } else {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            push_down(APP_CLOSE, c->id, NULL, 0);
            break;
        }
    }
    free(buf);

cleanup:
    /* 清理 */
    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    if (c->sock >= 0) { closesocket(c->sock); c->sock = -1; }
    c->used = 0;
    c->worker = NULL;
    xSemaphoreGive(s_conn_lock);
    vTaskDelete(NULL);
}

/* 真正去连目标 (阻塞, 由 opener_task 调用) */
static void do_open_blocking(uint32_t cid, const char *host, uint16_t port)
{
    /* 腾一个槽位 */
    conn_t *c = NULL;
    xSemaphoreTake(s_conn_lock, portMAX_DELAY);
    if (conn_find(cid)) {
        xSemaphoreGive(s_conn_lock);
        push_down(APP_OPENOK, cid, NULL, 0);
        return;
    }
    for (int i = 0; i < MAX_CONN; i++) {
        if (!s_conns[i].used) { c = &s_conns[i]; break; }
    }
    if (c) { c->used = 1; c->id = cid; c->sock = -1; c->closing = 0; c->worker = NULL; }
    xSemaphoreGive(s_conn_lock);

    if (!c) {
        push_down(APP_OPENFAIL, cid, NULL, 0);
        return;
    }

    /* 解析 + 连接 */
    struct addrinfo hints = { 0 }, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char portstr[8];
    snprintf(portstr, sizeof(portstr), "%u", port);

    int gai = getaddrinfo(host, portstr, &hints, &res);
    if (gai != 0 || !res) {
        ESP_LOGW(TAG, "DNS 失败 %s: %d", host, gai);
        xSemaphoreTake(s_conn_lock, portMAX_DELAY); c->used = 0; xSemaphoreGive(s_conn_lock);
        push_down(APP_OPENFAIL, cid, NULL, 0);
        return;
    }

    int s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s < 0) {
        freeaddrinfo(res);
        xSemaphoreTake(s_conn_lock, portMAX_DELAY); c->used = 0; xSemaphoreGive(s_conn_lock);
        push_down(APP_OPENFAIL, cid, NULL, 0);
        return;
    }

    struct timeval tv = { .tv_sec = 8, .tv_usec = 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    int r = connect(s, res->ai_addr, res->ai_addrlen);
    freeaddrinfo(res);

    if (r != 0) {
        ESP_LOGW(TAG, "connect %s:%u 失败 errno=%d", host, port, errno);
        closesocket(s);
        xSemaphoreTake(s_conn_lock, portMAX_DELAY); c->used = 0; xSemaphoreGive(s_conn_lock);
        push_down(APP_OPENFAIL, cid, NULL, 0);
        return;
    }

    c->sock = s;
    ESP_LOGI(TAG, "conn %lu -> %s:%u 已连接", (unsigned long)cid, host, port);
    push_down(APP_OPENOK, cid, NULL, 0);

    if (xTaskCreate(conn_worker, "cworker", WORKER_STACK, c, 5, &c->worker) != pdPASS) {
        ESP_LOGE(TAG, "worker 创建失败");
        closesocket(s);
        xSemaphoreTake(s_conn_lock, portMAX_DELAY); c->used = 0; xSemaphoreGive(s_conn_lock);
        push_down(APP_CLOSE, cid, NULL, 0);
    }
}

/* 处理 APP_OPEN: 连目标。
 * 注意: connect/getaddrinfo 会阻塞好几秒, 绝不能在隧道循环里同步做,
 * 否则 SPI 从机长时间处于"未武装"状态, master 读回全 0xFF。
 * 所以这里只把请求塞进队列, 由 opener 任务异步执行。 */
typedef struct { uint32_t cid; uint16_t hostlen; uint16_t port; char host[208]; } open_req_t;
static QueueHandle_t s_openq;

static void opener_task(void *arg)
{
    (void)arg;
    open_req_t req;
    for (;;) {
        if (xQueueReceive(s_openq, &req, portMAX_DELAY) != pdTRUE) continue;
        do_open_blocking(req.cid, req.host, req.port);
    }
}

static void do_open_async(uint32_t cid, const uint8_t *pl, uint16_t len)
{
    if (len < 4) { s_err++; push_down(APP_OPENFAIL, cid, NULL, 0); return; }

    uint16_t hostlen = pl[0] | (pl[1] << 8);
    if (hostlen == 0 || hostlen > 200 || len < 4 + hostlen) {
        s_err++; push_down(APP_OPENFAIL, cid, NULL, 0); return;
    }

    open_req_t r;
    r.cid = cid;
    r.hostlen = hostlen;
    r.port = pl[2 + hostlen] | (pl[2 + hostlen + 1] << 8);
    memcpy(r.host, pl + 2, hostlen);
    r.host[hostlen] = 0;

    if (xQueueSend(s_openq, &r, 0) != pdTRUE) {
        s_err++;
        push_down(APP_OPENFAIL, cid, NULL, 0);
    }
}

/* 处理 APP_DATA: master 发来的上行数据 */
static void do_data(uint32_t cid, const uint8_t *pl, uint16_t len)
{
    conn_t *c = conn_find(cid);
    if (!c || c->sock < 0) {
        s_err++;
        if ((s_err & 0xFF) == 1)
            ESP_LOGW(TAG, "do_data: conn %lu 不存在 (len=%u)",
                     (unsigned long)cid, len);
        return;
    }
    int sent = 0;
    while (sent < len) {
        int n = send(c->sock, pl + sent, len - sent, 0);
        if (n > 0) { sent += n; s_up++; }
        else if (errno == EAGAIN || errno == EWOULDBLOCK) { vTaskDelay(pdMS_TO_TICKS(2)); }
        else { ESP_LOGW(TAG, "conn %lu: send 失败 %d", (unsigned long)cid, errno); break; }
    }
}

/* 处理 APP_CLOSE: master 要关连接 */
static void do_close(uint32_t cid)
{
    conn_t *c = conn_find(cid);
    if (!c) return;
    c->closing = 1;
    if (c->sock >= 0) shutdown(c->sock, SHUT_RDWR);
}

/* ---------------- 初始化 ---------------- */

esp_err_t tunnel_init(void)
{
    s_downq = xQueueCreate(DOWNQ_LEN, sizeof(down_item_t));
    if (!s_downq) { ESP_LOGE(TAG, "downq 创建失败"); return ESP_ERR_NO_MEM; }

    s_openq = xQueueCreate(4, sizeof(open_req_t));
    if (!s_openq) { ESP_LOGE(TAG, "openq 创建失败"); return ESP_ERR_NO_MEM; }

    s_ipq = xQueueCreate(IPQ_LEN, sizeof(ip_item_t));
    if (!s_ipq) { ESP_LOGE(TAG, "ipq 创建失败"); return ESP_ERR_NO_MEM; }

    s_conn_lock = xSemaphoreCreateMutex();
    if (!s_conn_lock) return ESP_ERR_NO_MEM;

    memset(s_conns, 0, sizeof(s_conns));

    /* opener: 专门做阻塞的 connect, 不占用隧道循环 */
    if (xTaskCreate(opener_task, "opener", 5120, NULL, 6, NULL) != pdPASS) {
        ESP_LOGE(TAG, "opener 创建失败");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "tunnel ready: max %d conns, downq %d, ipq %d",
             MAX_CONN, DOWNQ_LEN, IPQ_LEN);
    return ESP_OK;
}

/* ---------------- 主循环 ---------------- */

void tunnel_task(void *arg)
{
    (void)arg;
    uint8_t *rx;
    int slot = 0;
    uint32_t nframes = 0;

    ESP_LOGI(TAG, "tunnel task start");

    /* 这里不需要预武装: spi_slave_init() 已经把 SLV_DEPTH 个槽位全部用
     * 合法 NODATA 帧头武装好了, 从开机起 master 就撞不到未武装空窗。 */

    for (;;) {
        /* Short timeout: a missed frame must be noticed quickly. Waiting the
         * old 200ms per lost frame is what capped the tunnel at ~18KB/s
         * (~183ms per data frame). */
        /* 等待下一帧。
         *
         * 超时 5ms: 原来是 200ms, 那会让一个丢帧卡住整条隧道 (实测
         * ~18KB/s)。内核主机轮询远快于用户态版本, 所以这里短超时 +
         * 立刻重试队列, 比长时间阻塞好得多。
         *
         * 注意不要改成 0: FreeRTOS 的 queue receive 用 0 表示"不等待",
         * 会让任务变成忙轮询烧 CPU, 反而挤占 lwIP 和 WiFi 任务的
         * 时间片 —— C5 是单核 240MHz, WiFi 协议栈本身就很吃 CPU。 */
        esp_err_t r = spi_slave_wait_rx(&rx, &slot, 5);
        if (r == ESP_ERR_TIMEOUT) continue;
        if (r != ESP_OK) {
            s_err++;
            if ((s_err & 0xFF) == 1)
                ESP_LOGW(TAG, "wait_rx 错误: %s", esp_err_to_name(r));
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        tun_hdr_t *h = (tun_hdr_t *)rx;

        /* ---- 1) 只暂存**真正要处理**的部分 ----
         *
         * 原来是每帧无条件 memcpy 整个载荷到 stage[]。但绝大多数帧是
         * 空心跳 (len=0), 那个 memcpy 什么也没搬, 纯浪费 —— 而这条
         * 路径每秒被调用上千次 (内核主机没有 Python 开销, 轮询更快)。
         *
         * 现在: len=0 时完全跳过拷贝。这是热路径上最直接的一笔节省。 */
        static uint8_t stage[TUN_FRAME_SIZE];
        uint16_t rlen = (h->len <= TUN_MAX_PAYLOAD) ? h->len : 0;
        uint8_t  rtype  = h->type;
        uint32_t rseq   = h->seq;
        uint16_t rcsum  = h->csum;
        uint32_t rmagic = h->magic;
        if (rlen) memcpy(stage, rx + TUN_HDR_SIZE, rlen);

        /* ---- 2) 复用刚完成的这个槽, 准备下一帧的应答 ----
         *
         * wait_rx 交回的槽已经出队, 可以直接覆写。不再需要 spi_slave_next()
         * 那种"翻缓冲" —— 槽本身就是缓冲。 */
        uint8_t *ntx = spi_slave_txbuf();
        tun_hdr_t *nth = (tun_hdr_t *)ntx;
        nth->magic    = TUN_MAGIC;
        nth->seq      = rseq;
        nth->type     = TUN_T_NODATA;
        nth->len      = 0;
        nth->csum     = 0;
        nth->flags    = 0;
        /* 捎带确认: 告诉 master 我们连续收到了哪个 seq。
         *
         * 注意: 主机的重传窗口虽然实测从未触发 (retx 恒为 0), 但这套
         * 机制仍然留在旧版 spinet.py 里。既然板上跑的是旧版主机,
         * 这个字段就必须继续按旧协议填, 否则主机的窗口会一直不推进。
         * 等主机侧也简化之后再一起拿掉。 */
        nth->reserved = (uint16_t)(s_ack_seq & 0xFFFF);

        if (rmagic == TUN_MAGIC && rtype == TUN_T_HELLO) {
            nth->type = TUN_T_HELLO;
            /* 主机重启后 seq 会重置, 而我们还记着很高的 s_ack_seq,
             * 于是后面的帧全被当成"太久以前的重复"丢掉。
             * 用这次 HELLO 的 seq 重同步窗口 (不是清零): 主机的 seq
             * 每次 HELLO 都自增, 清零会在 1..3 留一个永远补不上的洞。
             */
            s_ack_seq = rseq;
            s_ack_mask = 0;
            ESP_LOGW(TAG, "HELLO: resync ACK window at seq=%lu",
                     (unsigned long)rseq);
        } else {
            /* 每 ~1 秒塞一帧状态 (TUN_T_STAT) 给板子, 让网页能显示 C3 的
             * WiFi 信号/内存, 而不需要板子反过来 UDP 查 C3 —— 板子和 C3 之间
             * 本来就没有 IP 通路, 只有这条 SPI 隧道。 */
            static uint32_t last_stat = 0;
            uint32_t now = xTaskGetTickCount();
            if (now - last_stat >= pdMS_TO_TICKS(1000)) {
                last_stat = now;
                char stat[160];
                netlog_stat_line(stat, sizeof(stat));
                uint16_t sl = strlen(stat);
                if (sl > TUN_MAX_PAYLOAD) sl = TUN_MAX_PAYLOAD;
                nth->type = TUN_T_STAT;
                nth->len  = sl;
                memcpy(ntx + TUN_HDR_SIZE, stat, sl);
                nth->csum = tun_csum16((uint8_t *)stat, sl);
                goto armed;
            }
            /* Pack as many queued IP packets as fit into ONE frame.
             *
             * Measured: the link is stop-and-wait, so throughput is
             * bytes-per-round-trip, and one 1400-byte packet per 4080-byte
             * payload frame wasted most of the frame. With tun0's MTU at 1350,
             * three packets (3 x 1352 = 4056 bytes) fit, which nearly triples
             * the bytes per round trip for no extra protocol work.
             *
             * Layout: repeated [2B little-endian length][packet]. */
            ip_item_t ipit;
            down_item_t it;
            ip_item_t first;
            if (xQueueReceive(s_ipq, &first, 0) == pdTRUE) {
                uint16_t off = 0;
                ipit = first;
                for (;;) {
                    if ((uint32_t)off + 2 + ipit.len > TUN_MAX_PAYLOAD) {
                        /* Does not fit. Push it back for the next frame. */
                        xQueueSendToFront(s_ipq, &ipit, 0);
                        break;
                    }
                    ntx[TUN_HDR_SIZE + off]     = (uint8_t)(ipit.len & 0xFF);
                    ntx[TUN_HDR_SIZE + off + 1] = (uint8_t)((ipit.len >> 8) & 0xFF);
                    memcpy(ntx + TUN_HDR_SIZE + off + 2, ipit.data, ipit.len);
                    off += 2 + ipit.len;
                    free(ipit.data);
                    if (xQueueReceive(s_ipq, &ipit, 0) != pdTRUE) break;
                }
                nth->type = TUN_T_IP;
                nth->len  = off;
                nth->csum = tun_csum16(ntx + TUN_HDR_SIZE, off);
            } else if (xQueueReceive(s_downq, &it, 0) == pdTRUE) {
                /* 反向连接的数据由 rev_worker 填队列 */
                nth->type = TUN_T_APP;
                nth->len  = it.len;
                memcpy(ntx + TUN_HDR_SIZE, it.data, it.len);
                nth->csum = tun_csum16(it.data, it.len);
                free(it.data);
            }
        }

armed:
        /* ---- 3) 立刻武装 ----
         *
         * 这一步必须在"处理"之前: 一旦 queue_trans 入队, 主机随时可能
         * 开始传输, 此刻 s_tx[] 里的内容就是最终线上内容。处理慢了
         * 会撞上"未武装窗口", MISO 悬空读回 0xFF (实测丢 5/7 帧)。
         *
         * 传输长度固定 TUN_FRAME_SIZE —— 变长方案已回退, 原因见
         * spi_slave.c 里 spi_slave_arm() 的注释。
         */
        esp_err_t ar = spi_slave_arm_slot(slot);
        if (ar != ESP_OK) {
            ESP_LOGE(TAG, "arm 失败: %s", esp_err_to_name(ar));
            vTaskDelay(pdMS_TO_TICKS(1));
        }

        /* ---- 4) 现在才处理这一帧 (可以是慢操作) ---- */
        if (rmagic == TUN_MAGIC && rlen <= TUN_MAX_PAYLOAD) {
            const uint8_t *payload = stage;

            if (rlen == 0 || tun_csum16(payload, rlen) == rcsum) {
                /* 去重: 重传会让同一 seq 到达两次, 处理两次会把同一段数据
                 * 重复喂给连接 (乱码/重复内容) 或把同一个 IP 报文投递两次。
                 * T_APP 和 T_IP 共用主机的同一个 seq 空间, 所以共用一张
                 * 确认位图是对的。 */
                if ((rtype == TUN_T_APP || rtype == TUN_T_IP) && !ack_accept(rseq)) {
                    continue;            /* 重复帧, 已处理过 */
                }
                if (rtype == TUN_T_IP && rlen > 0) {
                    /* A frame can carry several IP packets: repeated
                     * [2B length][packet]. One packet per frame wasted most of
                     * the payload and, because the link is stop-and-wait,
                     * throughput is bytes-per-round-trip -- so packing more in
                     * is the single biggest win available. */
                    uint16_t off = 0;
                    while (off + 2 <= rlen) {
                        uint16_t plen = (uint16_t)(payload[off] |
                                                   (payload[off + 1] << 8));
                        off += 2;
                        if (plen < 20 || (uint32_t)off + plen > rlen) {
                            s_err++;          /* malformed -- drop the rest */
                            break;
                        }
                        s_ip_rx++;
                        spinet_input(payload + off, plen);
                        off += plen;
                    }
                } else if (rtype == TUN_T_APP && rlen >= APP_HDR_SIZE) {
                    uint8_t msg = payload[0];
                    uint32_t cid = payload[1] | (payload[2] << 8) |
                                   (payload[3] << 16) | (payload[4] << 24);
                    const uint8_t *body = payload + APP_HDR_SIZE;
                    uint16_t blen = rlen - APP_HDR_SIZE;

                    switch (msg) {
                    case APP_OPEN:  do_open_async(cid, body, blen);  break;
                    case APP_DATA:  do_data(cid, body, blen);        break;
                    case APP_CLOSE: do_close(cid);                   break;
                    default: s_err++; break;
                    }
                }
            } else {
                s_err++;
            }
        } else {
            s_err++;
        }

        if ((++nframes & 0x7FF) == 0) {
            ESP_LOGI(TAG, "frames=%lu up=%lu down=%lu drop=%lu err=%lu | ip tx=%lu rx=%lu drop=%lu free=%lu",
                     (unsigned long)nframes, (unsigned long)s_up,
                     (unsigned long)s_down, (unsigned long)s_drop,
                     (unsigned long)s_err,
                     (unsigned long)s_ip_tx, (unsigned long)s_ip_rx,
                     (unsigned long)s_ip_drop,
                     (unsigned long)esp_get_free_heap_size());
        }
    }
}

void tunnel_stats(uint32_t *up, uint32_t *down, uint32_t *drop, uint32_t *err)
{
    if (up)   *up   = s_up;
    if (down) *down = s_down;
    if (drop) *drop = s_drop;
    if (err)  *err  = s_err;
}

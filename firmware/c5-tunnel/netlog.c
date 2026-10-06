/*
 * netlog.c — UDP 日志/状态转发 (见 netlog.h 的设计说明)
 *
 * 关键点:
 *   - esp_log_set_vprintf() 是"链式"的: 我们保存原 handler, 先让它照常
 *     输出到串口, 再把同一份文本塞进队列走 UDP。
 *   - vprintf 可能在中断/任意任务上下文被调用, 里面**绝不能阻塞**,
 *     所以只用 xQueueSend(..., 0) 非阻塞投递, 满了直接丢。
 *   - 原始格式串不能直接用 (参数已展开), 所以先用 vsnprintf 格式化到
 *     栈缓冲。截断即可, 不动态分配。
 */
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "lwip/sockets.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_heap_caps.h"
#include "esp_heap_caps.h"

#include "netlog.h"

#define NETLOG_QLEN      24       /* 队列深度: 够吸收突发, 又不占太多堆 */
#define NETLOG_LINE_MAX  192      /* 单条日志上限, 超出截断 */
#define NETLOG_TX_STK    3072
/* 限速: 每秒最多转发这么多条日志。
 *
 * 日志是诊断手段, 绝不能影响隧道本身。C3 是单核 160MHz, 隧道 (SPI + WiFi)
 * 才是主业。实测不加限制时 log_drop 涨到 468941, C3 的 CPU 被日志格式化吃
 * 掉, 直接反映为控制指令时通时不通。 */
#define NETLOG_MAX_PER_SEC  20

typedef struct {
    uint16_t len;
    char     text[NETLOG_LINE_MAX];
} netlog_item_t;

static QueueHandle_t s_q;
static int           s_sock = -1;
static struct sockaddr_in s_dst;
static vprintf_like_t s_prev_vprintf;

/* 统计: 便于从串口侧确认转发是否真的在工作 */
static volatile uint32_t s_sent, s_dropped;

/* ---------------- 发送任务 ---------------- */

static void netlog_task(void *arg)
{
    (void)arg;
    netlog_item_t it;
    uint32_t win_start = xTaskGetTickCount();
    uint32_t win_count = 0;

    for (;;) {
        if (xQueueReceive(s_q, &it, pdMS_TO_TICKS(200)) != pdTRUE) continue;
        if (s_sock < 0) continue;

        /* 每秒最多发 NETLOG_MAX_PER_SEC 条, 保护隧道 */
        uint32_t now = xTaskGetTickCount();
        if ((now - win_start) >= pdMS_TO_TICKS(1000)) {
            win_start = now;
            win_count = 0;
        }
        if (win_count >= NETLOG_MAX_PER_SEC) {
            s_dropped++;
            continue;               /* 超限: 丢弃这条, 不占 CPU */
        }
        win_count++;

        int n = sendto(s_sock, it.text, it.len, 0,
                       (struct sockaddr *)&s_dst, sizeof(s_dst));
        if (n >= 0) s_sent++;
        else        s_dropped++;
    }
}

/* ---------------- 公共发送接口 ---------------- */

void netlog_send(const char *s, int len)
{
    if (!s_q || !s || len <= 0) return;

    netlog_item_t it;
    if (len > NETLOG_LINE_MAX - 1) len = NETLOG_LINE_MAX - 1;
    it.len = (uint16_t)len;
    memcpy(it.text, s, len);
    it.text[len] = '\0';

    /* 非阻塞: vprintf 钩子可能在任何上下文跑, 不能等 */
    if (xQueueSend(s_q, &it, 0) != pdTRUE) s_dropped++;
}

/* ---------------- vprintf 钩子 ---------------- */

static int netlog_vprintf(const char *fmt, va_list ap)
{
    /* 先照常输出到串口 (保持原有行为不变) */
    va_list ap2;
    va_copy(ap2, ap);
    int ret = s_prev_vprintf ? s_prev_vprintf(fmt, ap2) : 0;
    va_end(ap2);

    /* 队列满就别再格式化了。
     *
     * 实测教训: 日志队列只有 24 条, 没人监听 UDP 时它永远是满的, 于是每一条
     * 日志都在做一次 vsnprintf 然后丢弃 —— log_drop 涨到 468941, 白白烧掉
     * C3 的 CPU, 反过来拖慢 SPI 隧道 (表现为控制指令时通时不通)。
     * 先看队列有没有空间, 没有就直接返回, 不做任何格式化开销。 */
    if (!s_q || uxQueueSpacesAvailable(s_q) == 0) {
        s_dropped++;
        return ret;
    }

    char buf[NETLOG_LINE_MAX];
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    if (n > 0) {
        if (n > (int)sizeof(buf) - 1) n = sizeof(buf) - 1;
        netlog_send(buf, n);
    }
    return ret;
}

/* ---------------- 状态查询 + 初始化 ---------------- */

/* ---------------- 状态查询 ----------------
 *
 * 把当前 IP 用文件级静态变量记下来, 供状态应答使用。main.c 的循环会
 * 定期调用 netlog_set_ip() 更新它。 */
static char s_ip[16] = "0.0.0.0";

void netlog_set_ip(const char *ip)
{
    if (!ip) return;
    strncpy(s_ip, ip, sizeof(s_ip) - 1);
    s_ip[sizeof(s_ip) - 1] = '\0';
}

/* 监听 "stat" 请求并回一行 JSON。端口比日志端口大 1, 避免和日志流混淆。
 * 不依赖任何全局状态, 收到就问一次, 方便脚本轮询。 */
static void netlog_rx_task(void *arg)
{
    uint16_t port = (uint16_t)(uintptr_t)arg;
    int s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s < 0) { vTaskDelete(NULL); return; }

    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family      = AF_INET;
    a.sin_port        = htons(port);
    a.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
        close(s);
        vTaskDelete(NULL);
        return;
    }

    char rx[64];
    for (;;) {
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        int n = recvfrom(s, rx, sizeof(rx) - 1, 0,
                         (struct sockaddr *)&from, &fl);
        if (n <= 0) continue;
        rx[n] = '\0';
        if (strncmp(rx, "stat", 4) != 0) continue;

        char out[NETLOG_LINE_MAX];
        netlog_stat_line(out, sizeof(out));
        sendto(s, out, strlen(out), 0, (struct sockaddr *)&from, fl);
    }
}

/* ---------------- 状态行 ----------------
 *
 * 让 C3 自报状态, 不用插串口也能判断它健不健康:
 *   rssi      信号强度 (低于 -75 就要考虑天线/位置)
 *   sent/drop 日志转发是否跟得上 (drop 持续增长说明队列太小或网络堵)
 *   free      剩余堆 (内存泄漏会让它一路下滑)
 *   minfree   历史最低堆
 *   tasks     FreeRTOS 任务数 —— 反向连接每个都要起一个 worker 任务,
 *             连接结束后这个数字若持续上涨, 说明 worker 没被回收
 *   slots     反向代理槽位占用
 *   largest   最大连续空闲块 —— 与 free 一起看可以区分"真泄漏"和"碎片化":
 *             两者同步下降=真泄漏; free 降而 largest 降得更快=碎片化
 * uptime 用 tick 换算, 说明它有没有偷偷重启过。 */
void netlog_stat_line(char *out, int n)
{
    wifi_ap_record_t ap;
    int rssi = 0;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;

    snprintf(out, n,
             "{\"ip\":\"%s\",\"rssi\":%d,\"up_ms\":%lu,\"free\":%lu,\"minfree\":%lu,"
             "\"largest\":%lu,\"tasks\":%u,"
             "\"log_sent\":%lu,\"log_drop\":%lu}",
             s_ip, rssi,
             (unsigned long)(xTaskGetTickCount() * portTICK_PERIOD_MS),
             (unsigned long)esp_get_free_heap_size(),
             (unsigned long)esp_get_minimum_free_heap_size(),
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
             (unsigned)uxTaskGetNumberOfTasks(),
             (unsigned long)s_sent, (unsigned long)s_dropped);
}

/* ---------------- 初始化 ---------------- */

esp_err_t netlog_start(uint16_t port)
{
    if (port == 0) port = NETLOG_DEFAULT_PORT;

    s_q = xQueueCreate(NETLOG_QLEN, sizeof(netlog_item_t));
    if (!s_q) return ESP_ERR_NO_MEM;

    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s_sock < 0) return ESP_FAIL;

    /* 允许广播: 不设这个选项, 发到 x.x.x.255 会直接报错 */
    int bc = 1;
    setsockopt(s_sock, SOL_SOCKET, SO_BROADCAST, &bc, sizeof(bc));

    /* 用子网广播地址, 网段从当前 IP 推不出来也没关系 —— 直接发
     * 255.255.255.255 (受限广播) 在同一广播域内同样有效。 */
    memset(&s_dst, 0, sizeof(s_dst));
    s_dst.sin_family      = AF_INET;
    s_dst.sin_port        = htons(port);
    s_dst.sin_addr.s_addr = htonl(INADDR_BROADCAST);

    if (xTaskCreate(netlog_task, "netlog", NETLOG_TX_STK, NULL, 3, NULL) != pdPASS) {
        return ESP_FAIL;
    }

    /* 挂上钩子: 之后所有 ESP_LOGx 都会同时走 UDP */
    s_prev_vprintf = esp_log_set_vprintf(netlog_vprintf);

    /* 状态查询端口 = 日志端口 + 1 (向 C3:10000 发 "stat" 即可取回 JSON) */
    xTaskCreate(netlog_rx_task, "netlog_rx", NETLOG_TX_STK,
                (void *)(uintptr_t)(port + 1), 3, NULL);
    return ESP_OK;
}

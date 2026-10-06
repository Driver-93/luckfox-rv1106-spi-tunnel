/*
 * spi_slave.c -- ESP32-C5 GPSPI (SPI2) slave driver
 *
 * 关键时序问题 (实测 268/2997 帧回读全是 0xFF):
 *   master 是连续高速轮询的。如果 slave 在"处理完一帧"到"重新武装下一次
 *   传输"之间有间隙, master 正好在这段时间发起传输, MISO 就没人驱动,
 *   读回来全是 0xFF 或全零 (悬空)。
 *
 * ============================================================================
 * 为什么改成"多槽位预武装" (SLV_DEPTH)
 * ============================================================================
 * 旧实现只有 2 份缓冲, 而且**任何时候队列里只有一个 transaction**:
 *
 *     wait_rx()  ->  s_armed = false; 返回
 *     [空窗: 上层 memcpy + 组装应答, 几十微秒]
 *     arm()      ->  重新入队
 *
 * 表面看空窗很短, 但 queue_size=16 完全没起作用 —— 队列里永远只有 1 个。
 * 实测代价 (板子侧 dmesg, 每 30 秒采样一次):
 *
 *     frames=108843 ... fail=255 bad_magic=255 bad_csum=0
 *     frames=200038 ... fail=477 bad_magic=477 bad_csum=0
 *
 *   → fail == bad_magic 且 bad_csum 恒为 0, 说明不是位错 (信号完整性),
 *     而是**整帧全零** = MISO 没人驱动 = 撞上未武装窗口。
 *   → 约 0.93 次/秒, 占总帧数的 0.24%。
 *
 * 0.24% 听起来小, 但它落在 TCP 上就是一次重传: 网页实测延迟中位 69ms,
 * 最大 606ms —— 那个尖峰就是重传的 RTO。为了把它压掉, 必须让队列里
 * **始终有备用的已武装 transaction**, 这样 master 永远撞不到空窗。
 *
 * 新设计: SLV_DEPTH 个等价的 (tx,rx) 槽位, 启动时全部用合法 NODATA 帧头
 * 武装好。master 取走一个, 上层处理完就把**同一个槽**填上新应答再入队。
 * 由于上层每轮必定回一帧 (至少是 NODATA), 队列深度自然维持在 SLV_DEPTH,
 * 永不出现空窗。
 *
 * 深度的取舍: 入队是 FIFO, 上层填好的应答排在已有槽位之后, 所以要等
 * (SLV_DEPTH-1) 个 transaction 才能上总线。
 *   SLV_DEPTH=2 → 下行多 1 帧 (约 2.6ms), 只留 1 个备用槽, 抗抖动弱
 *   SLV_DEPTH=3 → 下行多 2 帧 (约 5.2ms), 2 个备用槽    ← 取这个
 *   SLV_DEPTH=4 → 下行多 3 帧 (约 7.8ms), 代价开始明显
 * 每个槽 4KB tx + 4KB rx, 3 个槽共 24KB —— C5 实测剩余堆 120KB, 够用。
 *
 * 另外: 4096B > FIFO(64B), 必须 DMA, 且缓冲要 word 对齐。
 */
#include <string.h>
#include "driver/spi_slave.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "spi_slave.h"

static const char *TAG = "spislv";

#define PIN_CS    10
#define PIN_CLK    6
#define PIN_MOSI   7
#define PIN_MISO   2

/* 预武装深度, 见文件头说明 */
#define SLV_DEPTH  3

static bool s_inited = false;

static WORD_ALIGNED_ATTR uint8_t s_tx[SLV_DEPTH][TUN_FRAME_SIZE];
static WORD_ALIGNED_ATTR uint8_t s_rx[SLV_DEPTH][TUN_FRAME_SIZE];

/* transaction 结构体必须在入队期间保持有效, 所以是静态数组不是局部变量 */
static spi_slave_transaction_t s_trans[SLV_DEPTH];

/* 当前正在被上层处理/组装应答的槽位 */
static int s_slot = 0;

/* 已入队、还没被 master 取走的槽位掩码。仅用于 is_armed() 诊断。 */
static uint32_t s_queued = 0;

esp_err_t spi_slave_arm_slot(int slot);

/* 由 rx_buffer 指针反查是哪个槽 —— master 可能取走任意一个备用槽,
 * 不能假设就是 s_slot。 */
static int slot_of(const uint8_t *rxbuf)
{
    for (int i = 0; i < SLV_DEPTH; i++)
        if (s_rx[i] == rxbuf) return i;
    return -1;
}

/* 用一个合法的 NODATA 帧头填满槽位。
 *
 * 关键: magic 必须写对。备用槽被 master 读到时, 它代表"这一轮从机没东西
 * 要发" —— 这是正常语义 (TUN_T_NODATA), 主机看到了就当空轮跳过。
 * 如果这里留全零, 主机就会记一次 bad_magic, 等于没修。 */
static void fill_nodata(uint8_t *tx)
{
    tun_hdr_t *h = (tun_hdr_t *)tx;
    memset(tx, 0, TUN_HDR_SIZE);
    h->magic = TUN_MAGIC;
    h->type  = TUN_T_NODATA;
    h->seq   = 0;
    h->len   = 0;
    h->csum  = 0;
}

esp_err_t spi_slave_init(void)
{
    if (s_inited) return ESP_OK;

    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_MOSI,
        .miso_io_num = PIN_MISO,
        .sclk_io_num = PIN_CLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = TUN_FRAME_SIZE,
    };
    spi_slave_interface_config_t slvcfg = {
        .mode = 0,
        .spics_io_num = PIN_CS,
        /* 队列深度 16。真正的"不留空窗"靠 SLV_DEPTH 个槽位轮流预武装
         * 实现 (见文件头), 这个值只要 >= SLV_DEPTH 即可。 */
        .queue_size = 16,
        .flags = 0,
        .post_setup_cb = NULL,
        .post_trans_cb = NULL,
    };

    esp_err_t r = spi_slave_initialize(SPI2_HOST, &buscfg, &slvcfg, SPI_DMA_CH_AUTO);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "spi_slave_initialize failed: %s", esp_err_to_name(r));
        return r;
    }

    gpio_set_pull_mode(PIN_CS, GPIO_PULLUP_ONLY);
    gpio_set_pull_mode(PIN_CLK, GPIO_PULLUP_ONLY);
    gpio_set_pull_mode(PIN_MOSI, GPIO_PULLUP_ONLY);

    /* ---- MISO 需要上拉 + 驱动能力 ----
     *
     * 实测症状: 主机侧读回的帧头是 **全零** (magic=0x00000000),
     * 而不是"数据位错"。全零意味着 MISO 线上没有任何驱动。
     *
     * 两个原因都会造成这个现象:
     *   1) 从机还没武装就被主机读了 (队列耗尽 -> 靠 SLV_DEPTH 解决)
     *   2) MISO 空闲时没有确定电平 —— ESP32 侧如果不主动驱动,
     *      线就悬空, 主机可能读回 0 或随机值
     *
     * 所以这里给 MISO 也加上拉: 即使从机来不及驱动, 空闲期间
     * 至少是确定的高电平, 而不是浮空的 0。 */
    gpio_set_pull_mode(PIN_MISO, GPIO_PULLUP_ONLY);

    /* 提高 MISO 的驱动强度。
     * 20MHz 下走线较长时, 默认驱动能力可能让上升沿变缓, 主机采样到
     * 错误电平。实测的 20MHz 上限本身就可能与信号完整性有关。 */
    gpio_set_drive_capability(PIN_MISO, GPIO_DRIVE_CAP_3);
    gpio_set_drive_capability(PIN_CLK,  GPIO_DRIVE_CAP_3);
    gpio_set_drive_capability(PIN_MOSI, GPIO_DRIVE_CAP_2);
    gpio_set_drive_capability(PIN_CS,   GPIO_DRIVE_CAP_2);

    memset(s_tx, 0, sizeof(s_tx));
    memset(s_rx, 0, sizeof(s_rx));
    s_slot = 0;
    s_queued = 0;
    s_inited = true;

    /* 全部槽位一次性预武装 —— 从这一刻起 master 永远撞不到空窗。
     * 上层 (tunnel_task) 不再需要开头的 arm()。 */
    for (int i = 0; i < SLV_DEPTH; i++) {
        fill_nodata(s_tx[i]);
        esp_err_t ar = spi_slave_arm_slot(i);
        if (ar != ESP_OK) {
            ESP_LOGE(TAG, "预武装槽 %d 失败: %s", i, esp_err_to_name(ar));
            return ar;
        }
    }

    ESP_LOGI(TAG, "slave ready: CS=%d CLK=%d MOSI=%d MISO=%d frame=%d depth=%d",
             PIN_CS, PIN_CLK, PIN_MOSI, PIN_MISO, TUN_FRAME_SIZE, SLV_DEPTH);
    return ESP_OK;
}

/* 把某个槽位入队等待 master 发起传输。调用前该槽的 tx 内容必须是最终线上内容。 */
esp_err_t spi_slave_arm_slot(int slot)
{
    if (!s_inited) return ESP_ERR_INVALID_STATE;
    if (slot < 0 || slot >= SLV_DEPTH) return ESP_ERR_INVALID_ARG;

    spi_slave_transaction_t *t = &s_trans[slot];
    memset(t, 0, sizeof(*t));

    /* 固定走满 TUN_FRAME_SIZE。
     *
     * 为什么不做变长 (踩过坑, 记录下来避免重犯):
     *   试过让从机用帧头 reserved 预告"下一帧长度", 主机跟随。结果是
     *   整条隧道死掉, C5 的 err 计数器每秒涨 ~1, ip tx/rx 恒为 0。
     *
     *   根本原因: reserved 只能表达"我这一帧要回发多少字节", 表达不了
     *   "我需要你发给我多少字节"。从机按自己的**应答**长度设传输长度,
     *   而上行 IP 包有 1350 字节 —— 当从机的应答是 16 字节心跳时,
     *   主机就没有足够的传输长度把包发出来, 于是数据永远卡在队列里。
     *   一个字段承载不了两个方向的需求, 这是设计错误, 不是实现 bug。
     *
     *   要正确做, 需要单独的"主机方向需求"字段 (比如复用 seq),
     *   从机取两者较大值。但那是一次真正的协议改动, 收益 (空载时省
     *   CPU) 不值一次掉线风险 —— 板上空载本来还有 33%。
     *
     * 所以回到定长。这是经过验证的、可用的行为。 */
    t->length    = TUN_FRAME_SIZE * 8;
    t->tx_buffer = s_tx[slot];
    t->rx_buffer = s_rx[slot];

    esp_err_t r = spi_slave_queue_trans(SPI2_HOST, t, 0);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "queue_trans 槽 %d 失败: %s", slot, esp_err_to_name(r));
        return r;
    }
    s_queued |= (1u << slot);
    return ESP_OK;
}

/* 兼容旧接口: 武装"当前槽"。新代码请直接用 spi_slave_arm_slot()。 */
esp_err_t spi_slave_arm(void)
{
    return spi_slave_arm_slot(s_slot);
}

/* 设置传输长度。变长方案已废弃, 保留空实现以免上层还要改。 */
void spi_slave_set_txlen(uint32_t n)
{
    (void)n;
}

/*
 * 等一帧完成。
 *   *rxbuf    -> 收到的数据
 *   *slot_out -> 是哪个槽完成的 (调用者用 spi_slave_txbuf() 复用这个槽回复)
 *
 * 返回后该槽已不在队列里, 上层应尽快组装应答并 spi_slave_arm_slot(slot)
 * 重新入队, 队列深度就回到 SLV_DEPTH。
 *
 * 注意: master 取走的可能是任意一个备用槽, 所以这里必须用 slot_of() 反查,
 * 不能假设是 s_slot。
 */
esp_err_t spi_slave_wait_rx(uint8_t **rxbuf, int *slot_out, int timeout_ms)
{
    if (!s_inited) return ESP_ERR_INVALID_STATE;

    spi_slave_transaction_t *done = NULL;
    esp_err_t r = spi_slave_get_trans_result(SPI2_HOST, &done, pdMS_TO_TICKS(timeout_ms));
    if (r != ESP_OK) return r;

    int i = slot_of((const uint8_t *)done->rx_buffer);
    if (i < 0) {
        ESP_LOGE(TAG, "完成的 transaction 找不到对应槽 (rx_buffer=%p)", done->rx_buffer);
        return ESP_ERR_INVALID_STATE;
    }

    s_queued &= ~(1u << i);
    s_slot = i;
    if (rxbuf)    *rxbuf = s_rx[i];
    if (slot_out) *slot_out = i;
    return ESP_OK;
}

uint8_t *spi_slave_txbuf(void) { return s_tx[s_slot]; }
uint8_t *spi_slave_rxbuf(void) { return s_rx[s_slot]; }

/* 旧接口: 切缓冲。槽位池实现下不需要了 —— wait_rx 已经把 s_slot 指向
 * 刚完成、可以复用的槽。保留空实现兼容旧调用点。 */
void spi_slave_next(void) { }

bool spi_slave_is_armed(void) { return s_queued != 0; }

int spi_slave_queued_count(void)
{
    int n = 0;
    for (int i = 0; i < SLV_DEPTH; i++) if (s_queued & (1u << i)) n++;
    return n;
}

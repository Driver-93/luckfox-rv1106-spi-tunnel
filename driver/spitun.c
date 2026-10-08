/*
 * spitun.c — SPI 隧道内核模块
 *
 * 目标: 把 car/spinet.py 的用户态隧道搬进内核, 消掉每帧 1.46ms 的固定开销。
 *
 * ---- 为什么值得做 (实测数据) ----
 *
 * 板子上实测单次 SPI exchange 的耗时 (4096 字节帧 @ 20MHz):
 *
 *     纯线上时间     1.64 ms   (4096 * 8 / 20e6)
 *     实测总耗时     3.10 ms
 *     ─────────────────────
 *     净开销         1.46 ms   (47%)
 *
 * 而那 1.46ms 全部来自用户态边界:
 *     ioctl 系统调用 + 用户态/内核态数据拷贝 x2 + Python 解释器 + 调度唤醒
 *
 * 内核里这些全部消失: 直接调 SPI 子系统, DMA 直接读写, 中断直接处理,
 * 而且可以连续提交多帧而不逐帧等待 —— 不再受 stop-and-wait 的往返限制。
 *
 * ---- 协议 (必须与 C5 固件 spi_tunnel_c3/main/tunnel.h 完全一致) ----
 *
 * 帧头 16 字节, 小端:
 *     off  0  u32 magic     0x3254464C
 *     off  4  u8  type      1=HELLO 3=NODATA 4=STAT 5=IP
 *     off  5  u8  flags
 *     off  6  u16 reserved  (从机用它/累积 ACK)
 *     off  8  u32 seq
 *     off 12  u16 len       载荷长度
 *     off 14  u16 csum      sum(bytes) % 65521
 *
 * T_IP 帧载荷: 反复 [2B 小端长度][IP 报文], 与 tunnel.c 的 pack 逻辑一致。
 *
 * ---- 传输长度: 固定 4096 ----
 *
 * 这是踩过坑之后定下来的, 不要改成变长:
 *   SPI 全双工、时钟由主机打, 所以**一个长度同时约束两个方向**。
 *   变长方案需要从机告诉主机"该给我多少字节", 但帧头里没有能同时表达
 *   两个方向需求的字段 —— 试过用 reserved, 结果整条隧道死锁
 *   (从机按自己的应答长度设传输, 主机就没长度发 1350 字节的 IP 包了)。
 *
 *   定长的代价是空帧也走 4096 字节; 但内核里这 1.64ms 是 DMA 在搬,
 *   CPU 不参与, 而且不再有每帧的系统调用开销。
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/netdevice.h>
#include <linux/if_tun.h>
#include <linux/spi/spi.h>
#include <linux/skbuff.h>
#include <linux/inetdevice.h>
#include <linux/workqueue.h>
#include <linux/kthread.h>
#include <linux/delay.h>
#include <linux/version.h>
#include <linux/inet.h>
#include <linux/ip.h>
#include <net/ip.h>
#include <net/route.h>
#include <net/fib_rules.h>

#define DRV_NAME    "spitun"

/* ---- 协议常量 (与 C5 的 tunnel.h 一致) ---- */
#define FRAME_SIZE      4096
#define HDR_SIZE        16
#define MAX_PAYLOAD     (FRAME_SIZE - HDR_SIZE)

#define TUN_MAGIC       0x3254464CUL

#define T_HELLO         0x01
#define T_APP           0x02
#define T_NODATA        0x03
#define T_STAT          0x04
#define T_IP            0x05

/* 线上帧头 */
struct tun_hdr {
    __le32 magic;
    u8     type;
    u8     flags;
    __le16 reserved;
    __le32 seq;
    __le16 len;
    __le16 csum;
} __packed;

/* 从机 C5 的 ACK 窗口是 64; 主机在途帧数不要超过它 */
#define ACK_WIN         64
/* 一帧最多装几个 IP 报文 (1350 * 3 + 3*2 = 4056 <= 4080) */
#define MAX_PKTS_PER_FRAME  3

/* ---- 帧级重传 (2026-10-05 第十八轮) ----
 *
 * SPI 是同步总线, 但**时序竞争仍会丢整帧**: 从机处理完上一帧到重新"武装"
 * 之间有窗口, 主机撞上就读回全零 -> bad_magic。实测:
 *    稳态 ~0.6~4%(累计), 但板子/C5 刚上电或负载突变时会 burst 到 **30%**
 *    (实测 30 秒里 12316 帧, 3715 帧失败)。
 *
 * 隧道层**没有重传**, 丢一帧 = 那一帧里的 IP 报文永久消失。主机这边只能靠
 * TCP 自个儿重传, 而 Linux 的 RTO 起步就是 **200ms**, 指数退避能到几秒 ——
 * 用户看到的就是"丢包 / 控制一顿一顿 / 延迟忽高忽低"。
 *
 * 修法很直接: **失败就在最底层立刻重发同一帧**。代价是一帧 (~3ms = 线上
 * 1.6ms + 固定开销), 换掉 TCP 的 200ms+。而且只需要改板子这一侧 ——
 * 从机那边"这一帧根本没武装", 说明它并没有发出去数据, 重发不会重复。
 *
 * 重复报文是安全的: 重发会让同一帧到达两次, 但里面是 IP 报文, TCP 层按
 * 序号去重, UDP 侧 (WebRTC/RTP) 本来就能容忍重复。
 */
#define EXCH_RETRY      3

/* 进程上下文: 每个发送周期最多尝试几次。
 * 用"周期起点"计时而不是每次 usleep, 这样即使一直失败也不会拖慢主循环。 */
static unsigned long retry_win_start;   /* jiffies */
static unsigned int retry_win_used;

/* 允许重试的窗口 (jiffies)。一个发送周期正常约 3ms, 给到 2 倍余量。 */
#define RETRY_CYCLE_JIFFIES   max_t(unsigned long, 1, HZ / 150)

/* 每个窗口最多重试几次 —— 防止 C5 整机不在线时无限空转。
 * 实测教训: 复位 C5 期间 bad_magic=6183, 我的第一次实现**每次都重试 3 遍**,
 * 每遍还要 usleep 0.2~0.4ms, 全打在主循环上; 而那种情况根本救不回来
 * (从机整个不在), 纯属浪费。现在: 有预算才重试, 没预算就算了, 交给上层。 */
#define RETRY_BUDGET_PER_CYCLE  4

static bool retry_budget_take(void)
{
    if (!retry_win_start || time_after(jiffies, retry_win_start)) {
        retry_win_start = jiffies + RETRY_CYCLE_JIFFIES;
        retry_win_used = 0;
    }
    if (retry_win_used >= RETRY_BUDGET_PER_CYCLE)
        return false;
    retry_win_used++;
    return true;
}

/* 统计 */
static unsigned long stat_tx_frames, stat_rx_frames;
static unsigned long stat_errors, stat_ip_tx, stat_ip_rx, stat_ip_drop;
static unsigned long stat_retry, stat_retry_ok, stat_retry_skip;
/* 发送队列相关 (定义在下面的队列那一节里用, 但 tun_stats 需要在这里就能看到) */
static unsigned long stat_hi_enq, stat_hi_drop, stat_tx_pack;

/* ---- 诊断计数 (定位"链路在跑但 IP 不通"这类问题) ----
 *
 * 之前每次出问题都只能靠改代码重刷固件试探, 因为日志里没有任何
 * 关于"收到什么类型的帧"的信息。这几个计数器把接收路径拆开:
 *   stat_rx_ok / stat_rx_fail    —— exchange 成功/失败
 *   stat_rx_ip_frames            —— 收到 T_IP 帧 (真正承载 IP 报文)
 *   stat_rx_stat_frames          —— 收到 T_STAT 帧 (C5 状态)
 *   stat_rx_hello / other        —— 其它
 * 如果 STAT 在涨而 IP 不动, 说明 C5 没往这边发 IP 数据;
 * 如果都停, 说明 SPI 层断了。 */
static unsigned long stat_rx_ok, stat_rx_fail;
static unsigned long stat_rx_ip_frames, stat_rx_stat_frames;
static unsigned long stat_rx_hello, stat_rx_other;
static unsigned long stat_next_report;
static unsigned long stat_bad_magic, stat_bad_csum;

/* SPI 设备 */
static struct spi_device *spi_dev;

/* SPI 时钟频率 (Hz)。
 *
 * 默认 20MHz, 但实测飞线 + 无屏蔽时这个速度跑不动:
 * Luckfox 侧读到恒定错误 magic (0x52951153), C5 侧每帧 err,
 * 两边 frames 都在涨但数据全错 —— 典型的采样错位。
 *
 * 改成模块参数, 可以 insmod 时指定, 不用重编:
 *     insmod spitun.ko spi_speed=1000000     # 1MHz
 *     insmod spitun.ko spi_speed=100000      # 100kHz
 *     insmod spitun.ko spi_speed=5000000     # 5MHz
 */
static u32 spi_speed_hz = 20000000;
module_param(spi_speed_hz, uint, 0644);
MODULE_PARM_DESC(spi_speed_hz, "SPI clock in Hz (default 20000000)");


static struct net_device *tun_netdev;
static struct task_struct *spitun_thread;

/* DMA 缓冲 —— 必须 cacheline 对齐, SPI 子系统会做 DMA。
 *
 * 用 kmalloc 保证物理连续 (4096 字节远小于一页)。不需要单独保存
 * dma_addr_t: SPI 子系统在 spi_sync 内部自己映射 tx_buf/rx_buf。 */
static u8 *txbuf;
static u8 *rxbuf;

/* 组帧暂存区 (TX 方向打包多个报文用)。
 * 只有 spitun_thread 一个使用者, 所以静态即可; spitun_exchange 还会把它
 * 拷进 txbuf, 这里不需要对齐要求。 */
static u8 txpack[MAX_PAYLOAD];

/* 注意: 这里**故意没有** seq 计数器。
 * 所有帧的 seq 恒为 0 —— 递增 seq 会激活 C5 的滑动窗口, 一旦丢帧就
 * 留下补不上的洞, 导致隧道永久死锁。详见 spitun_exchange() 里的说明。 */

/* 状态帧 (来自 C5) 最近一次的内容, 供调试 */
static char last_stat[256];
static DEFINE_MUTEX(stat_lock);

/* ---- 把 C5 状态暴露给用户态 ----
 *
 * 为什么需要: 原来用户态的 spinet.py 会把 C5 的状态帧写进
 * /tmp/c3_state.json, 网页 (web_server.py) 读这个文件来显示 WiFi 信号。
 *
 * 隧道搬进内核后这个文件不再产生, 于是网页永远显示
 *     "c3": {"online": false}
 * 看起来像 WiFi 掉线, 其实 C5 连得好好的 —— 只是没人把状态写出去。
 *
 * 内核模块不能让用户态随便读一块内存, 所以走 sysfs:
 *     cat /sys/class/net/spitun0/c3_status
 * web_server.py 改成读这个文件即可 (不动固件的部分)。
 *
 * 用 sysfs 而不是 procfs: 这个属性属于 spitun0 这个网络设备,
 * 挂在设备下面语义最清楚, 也不用额外注册 proc 目录。
 */
static ssize_t c3_status_show(struct device *dev,
                              struct device_attribute *attr, char *buf)
{
    ssize_t n;

    mutex_lock(&stat_lock);
    n = scnprintf(buf, PAGE_SIZE, "%s\n",
                  last_stat[0] ? last_stat : "");
    mutex_unlock(&stat_lock);

    return n;
}
static DEVICE_ATTR_RO(c3_status);

/* ---- 隧道自己的计数器, 走 sysfs ----
 *
 * 为什么不用 dmesg: 一是周期日志要等 30 秒, 二是实测**这一版的长行根本没
 * 打印出来**(16 个 %lu 的那条 pr_info 一次都没出现在 ring buffer 里,
 * 短行都正常)。排查丢包时最需要的就是"随时能读一眼计数器", 所以直接挂
 * 成设备属性:
 *
 *     cat /sys/class/net/spitun0/tun_stats
 *
 * 一次读到的都是**累计值**, 差值自己算 (取两次相减)。
 */
static ssize_t tun_stats_show(struct device *dev,
                              struct device_attribute *attr, char *buf)
{
    return scnprintf(buf, PAGE_SIZE,
                     "frames=%lu ok=%lu fail=%lu bad_magic=%lu bad_csum=%lu\n"
                     "ip_tx=%lu ip_rx=%lu ip_drop=%lu\n"
                     "hi_enq=%lu hi_drop=%lu multipack=%lu\n"
                     "retry=%lu retry_ok=%lu retry_skip=%lu\n",
                     stat_tx_frames, stat_rx_ok, stat_rx_fail,
                     stat_bad_magic, stat_bad_csum,
                     stat_ip_tx, stat_ip_rx, stat_ip_drop,
                     stat_hi_enq, stat_hi_drop, stat_tx_pack,
                     stat_retry, stat_retry_ok, stat_retry_skip);
}
static DEVICE_ATTR_RO(tun_stats);

static struct attribute *spitun_attrs[] = {
    &dev_attr_c3_status.attr,
    &dev_attr_tun_stats.attr,
    NULL,
};

/* 只定义 attribute_group 本身, 不用 ATTRIBUTE_GROUPS()。
 *
 * ATTRIBUTE_GROUPS() 会额外生成一个 spitun_groups[] 指针数组, 而
 * net_device.sysfs_groups[] 每个槽位要的是**单个指针**
 *     const struct attribute_group *sysfs_groups[4];
 * 用那个数组会报 "incompatible pointer type", 而且数组本身没人用,
 * 又会触发 -Werror=unused-variable。所以手写结构体最干净。 */
static const struct attribute_group spitun_group = {
    .attrs = spitun_attrs,
};

/* 关机标志 */
static bool spitun_shutdown;

/* ================= 校验和 =================
 *
 * 必须与 tunnel.h 的 tun_csum16() 和 spinet.py 的 csum16() 一致:
 *     zlib.adler32(b) & 0xFFFF == (1 + sum(bytes)) % 65521
 *
 * 在用户态这是一个逐字节的 Python 循环 (4056 字节要 0.55ms, 每次交换算两遍),
 * 是链路上最大的单项开销。在 C 里它几乎免费。
 */
static u16 tun_csum16(const u8 *p, size_t n)
{
    u32 s = 1;
    size_t i;

    for (i = 0; i < n; i++)
        s += p[i];
    return (u16)(s % 65521u);
}

/* ================= 一次 SPI 收发 =================
 *
 * 提交一帧并等它回来。这是隧道的心跳: C5 是 SPI 从机, 只有主机发起传输时
 * 它才能回数据, 所以即使没东西要发也必须持续轮询, 否则 C5 的下行数据
 * 永远出不来。
 *
 * 返回: 0 成功, 负值失败。
 * 成功时 out_type 与 out_len 描述应答, 载荷在 rxbuf 里。
 *
 * ---- seq 的处理非常关键 (用 ip rx 恒为 0 换来的) ----
 *
 * 只有 T_IP / T_APP 帧才分配序号, **心跳和状态帧永远用 seq = 0**。
 *
 * 原因在 C5 的 ack_accept():
 *     if (seq == 0) return 1;              // 无序号帧直接放行
 *     d = seq - s_ack_seq;
 *     if (d <= 0) return 0;                // 老的 -> 丢弃
 *     if (d > ACK_WIN) return 0;           // 超出窗口 -> 丢弃
 * 而 C5 **只在收到 T_IP/T_APP 时才调 ack_accept**, 心跳不推进 s_ack_seq。
 *
 * 所以如果心跳也消耗序号, 会出现:
 *     心跳每 3ms 一个, seq 飞快涨到 100+
 *     C5 的 s_ack_seq 还是 0
 *     第一个 T_IP 到达时 d = 100 > ACK_WIN(64) -> 被当"太远"丢弃
 *     => ip rx 永远是 0
 *
 * 最阴险的是它**不报错**: ack_accept 返回 0 走的是 continue, 不增加 err,
 * 所以日志上错误率只有 0.08% 看着很健康, 实际上一个 IP 包都没过去。
 */
static int spitun_exchange(u8 type, const u8 *payload, size_t len,
                           u8 *out_type, size_t *out_len)
{
    struct spi_transfer xfer = { 0 };
    struct spi_message msg;
    struct tun_hdr *h = (struct tun_hdr *)txbuf;
    u16 csum;
    int ret;

    /* 必须有设备才能传输。
     *
     * 这个检查是**用内核崩溃换来的**: 隧道线程在 probe 之前就启动了, 而
     * spi_dev 还是 NULL。spi_sync(NULL, ...) 直接解引用空指针 ->
     *     CPU: 0 PID: 306 Comm: spitun
     *     LR is at spitun_exchange+0xad/0x128 [spitun]
     *     [<spi_sync>] from [<spitun_exchange>] from [<spitun_loop>]
     * 整个隧道线程死掉, 而 ip link 显示 spitun0 还是 UP 的, 很容易误判。
     *
     * 现在返回 -ENODEV, 让 spitun_loop 安静地重试, 等 probe 完成后自然恢复。
     */
    if (!spi_dev)
        return -ENODEV;

    if (len > MAX_PAYLOAD)
        len = MAX_PAYLOAD;

    csum = len ? tun_csum16(payload, len) : 0;

    /* 写帧头。注意 txbuf 是复用的, 所以每次都重写完整帧头。
     * 载荷尾部不必要清零 —— 从机只读 len 指定的字节数。 */
    h->magic    = cpu_to_le32(TUN_MAGIC);
    h->type     = type;
    h->flags    = 0;
    h->reserved = 0;

    /* seq 恒为 0 —— 这是**必须**的, 用整条隧道的猝死换来的。
     *
     * ---- 为什么不能用递增 seq ----
     *
     * C5 侧 ack_accept() 有滑动窗口:
     *     if (seq == 0) return 1;             // 无序号帧直接放行
     *     d = seq - s_ack_seq;
     *     if (d > ACK_WIN) return 0;          // 超出窗口 (64) 直接丢
     *
     * SPI 是同步总线, 但**时序竞争仍会丢帧**: 从机在处理上一帧到重新
     * 武装之间有窗口, 主机撞上就读回全零。实测 bad_magic 约 2%。
     *
     * 一旦某一帧丢了, 就留下一个**永远补不上的洞**:
     *     seq 100 丢失 -> s_ack_seq 卡在 99
     *     seq 101..163 -> d=2..64, 收下但窗口不推进
     *     seq 164+     -> d>64, 全部丢弃 -> 永久死锁
     *
     * 而本模块**没有重传** (用户态版本实测 retx 恒为 0, 据此删掉了),
     * 所以洞填不上。症状: 隧道能用几秒到几分钟, 然后 ip tx/rx 永久冻结
     * (C5 的 frames 还在涨, err 也不涨 —— 看起来一切正常, 实际已死)。
     *
     * ---- 为什么 seq=0 是正确解法而不是妥协 ----
     *
     * 这套 seq/窗口机制在本模块里是纯累赘:
     *   * 本模块**不读** C5 回的累积 ACK (reserved 字段), 不依赖窗口
     *   * SPI 是同步总线, 本来就不需要"可靠传输"
     *   * 丢帧由 TCP 自己重传 —— 这才是分层的正确做法
     * 也就是说它唯一的作用就是制造死锁。恒为 0 即彻底移除这个失效模式。 */
    h->seq      = 0;
    h->len      = cpu_to_le16((u16)len);
    h->csum     = cpu_to_le16(csum);

    if (len)
        memcpy(txbuf + HDR_SIZE, payload, len);

    /* 固定长度传输。
     *
     * 为什么不做变长: 见文件头的说明。SPI 全双工、长度只有一个,
     * 而帧头里没有字段能同时表达"我要发多少"和"我需要你收多少"。
     * 变长方案试过, 会让隧道死锁。 */
    xfer.tx_buf = txbuf;
    xfer.rx_buf = rxbuf;
    xfer.len    = FRAME_SIZE;
    xfer.speed_hz = spi_speed_hz;

    spi_message_init(&msg);
    spi_message_add_tail(&xfer, &msg);

    ret = spi_sync(spi_dev, &msg);
    if (ret < 0) {
        stat_errors++;
        return ret;
    }

    /* 解析应答。只按帧头声明的长度取数据, 不做整帧拷贝。 */
    {
        const struct tun_hdr *rh = (const struct tun_hdr *)rxbuf;
        u16 rlen = le16_to_cpu(rh->len);
        u32 rmagic = le32_to_cpu(rh->magic);

        /* 两个失败原因分开报, 否则只知道"帧不对"却不知道是不对在哪:
         *   magic 错  -> 根本不是我们的帧 (SPI 时序/接线/从机没武装)
         *   csum 错   -> 帧头对但数据坏了 (长度对不上/传输被截断)
         * 这两种问题的排查方向完全不同, 之前混在一个 -EBADMSG 里
         * 浪费了大量时间。 */
        if (rmagic != TUN_MAGIC || rlen > MAX_PAYLOAD) {
            stat_bad_magic++;
            if (stat_bad_magic <= 3 || (stat_bad_magic % 500) == 0)
                pr_warn(DRV_NAME ": bad magic 0x%08x (want 0x%08x) "
                        "len=%u rlen=%u seq=%u type=%u (#%lu)\n",
                        rmagic, (u32)TUN_MAGIC, (unsigned)len,
                        (unsigned)le16_to_cpu(rh->len),
                        (unsigned)le32_to_cpu(rh->seq),
                        (unsigned)rh->type, stat_bad_magic);
            stat_errors++;
            return -EBADMSG;
        }
        if (rlen && tun_csum16(rxbuf + HDR_SIZE, rlen) !=
                    le16_to_cpu(rh->csum)) {
            stat_bad_csum++;
            if (stat_bad_csum <= 3 || (stat_bad_csum % 500) == 0)
                pr_warn(DRV_NAME ": bad csum type=%u rlen=%u (#%lu)\n",
                        (unsigned)rh->type, (unsigned)rlen, stat_bad_csum);
            stat_errors++;
            return -EBADMSG;
        }

        if (out_type)
            *out_type = rh->type;
        if (out_len)
            *out_len = rlen;
    }

    stat_rx_frames++;
    return 0;
}

/* ================= 发送队列 (双队列 + 每帧打包) =================
 *
 * xmit 在原子上下文里入队, spitun_thread 在进程上下文里出队并真正发送。
 *
 * 为什么不用 skb_queue / 直接转发 skb: 隧道线程要的是"已经打好
 * [2B len][packet] 的字节块", 在 xmit 里组好再入队, 线程侧就只需要
 * memcpy + spi_sync, 逻辑最简单, 也避免在原子上下文里做更多事。
 *
 * ---- 2026-10-05 第 23 轮: 控制延迟专项, 两个改动 ----
 *
 * 实测背景 (100ms 心跳, keep-alive 单连接, 板载 /api/move):
 *     空载        : RTT p50 23ms / max 45ms
 *     HTTP 满载   : RTT p50 95ms / p90 121ms / max 151ms   (3.4 Mbps 竞争流量)
 * 而 3.4 Mbps 远低于"隧道能搬的字节数", 所以瓶颈不是带宽, 是**排队**。
 *
 * (1) 每帧打包多个报文 --- 原来一次 exchange (3.1ms, 固定 4096B 帧) 只塞
 *     **一个** ≤1350B 的报文, 也就是 3ms 才过一个包 (≈333 pkt/s)。
 *     帧里还空着 2/3。C5 侧的 tunnel.c 早就在这个方向上打包 (它按
 *     [2B len][packet] 重复解析, 一次最多 3 个), 板子这边却没打 ——
 *     纯浪费。现在一次最多塞 3 个 (3×1352 = 4056 ≤ 4080), 包速率翻 3 倍,
 *     队列排空快 3 倍, 排队延迟也小 3 倍。**不需要改 C5 固件**。
 *
 * (2) 小包优先队列 --- 视频/图传是大包 (1350B), 控制指令和它的 TCP ACK
 *     只有几十字节, 却和视频**排在同一个 FIFO** 里。队列深 64 时, 一个
 *     控制包最坏要等 64 个大包 = 190ms。分两级后, 小包 (≤256B, 覆盖
 *     /api/move 的请求与响应、以及所有纯 ACK) 直接插到大包前面。
 *     小包本来就少, 不会饿死大包。
 *
 * 深度: 原来 64。**实测不够** —— 图传/HTTP 突发时队列瞬间打满,
 * `ip_drop` 一篇测试里涨了 593 个包, 那些都是 TCP 段, 上层只能重传
 * (图传卡顿 + 控制抖动都从这里来)。每个条目最多 1352 字节, 256 条约
 * 346KB; 板子有 100MB+ 空闲内存, 这点占用换来"突发不丢包"很划算。
 *
 * 高优先级队列只有小包 (控制/ACK), 32 条足够, 不需要跟着放大 ——
 * 它的意义是"插队", 不是"堆量"。
 */
#define TXQ_LEN     256     /* 普通队列 (批量数据/图传) */
#define TXQ_HI_LEN  32      /* 高优先级队列 (小包/控制/ACK) */
#define TXQ_HI_MAX  256     /* 载荷 ≤ 256B 视为小包 (条目长度 = 载荷+2) */

struct tx_item {
    u16 len;
    u8 *data;
};
static struct tx_item txq[TXQ_LEN];
static unsigned int txq_head, txq_tail, txq_count;
static struct tx_item txq_hi[TXQ_HI_LEN];
static unsigned int hi_head, hi_tail, hi_count;
static DEFINE_SPINLOCK(txq_lock);

/* 原子上下文安全: 只用自旋锁, 不睡眠。返回 false 表示对应队列满。 */
static bool spitun_enqueue(const u8 *data, u16 len)
{
    unsigned long flags;
    bool ok = false;
    bool hi = (len <= TXQ_HI_MAX + 2);

    spin_lock_irqsave(&txq_lock, flags);
    if (hi) {
        if (hi_count < TXQ_HI_LEN) {
            txq_hi[hi_tail].data = (u8 *)data;      /* 接管所有权 */
            txq_hi[hi_tail].len  = len;
            hi_tail = (hi_tail + 1) % TXQ_HI_LEN;
            hi_count++;
            stat_hi_enq++;
            ok = true;
        } else {
            stat_hi_drop++;
        }
    } else if (txq_count < TXQ_LEN) {
        txq[txq_tail].data = (u8 *)data;
        txq[txq_tail].len  = len;
        txq_tail = (txq_tail + 1) % TXQ_LEN;
        txq_count++;
        ok = true;
    }
    spin_unlock_irqrestore(&txq_lock, flags);

    return ok;
}

/* 进程上下文: 取一个待发条目; 没有则返回 false。 高优先级队列优先。 */
static bool spitun_dequeue(u8 **data, u16 *len)
{
    unsigned long flags;
    bool ok = false;

    spin_lock_irqsave(&txq_lock, flags);
    if (hi_count > 0) {
        *data = txq_hi[hi_head].data;
        *len  = txq_hi[hi_head].len;
        hi_head = (hi_head + 1) % TXQ_HI_LEN;
        hi_count--;
        ok = true;
    } else if (txq_count > 0) {
        *data = txq[txq_head].data;
        *len  = txq[txq_head].len;
        txq_head = (txq_head + 1) % TXQ_LEN;
        txq_count--;
        ok = true;
    }
    spin_unlock_irqrestore(&txq_lock, flags);

    return ok;
}

static void spitun_txq_flush(void)
{
    u8 *d;
    u16 l;

    while (spitun_dequeue(&d, &l))
        kfree(d);
}

/* ---- 组一帧: 把小包优先、最多 MAX_PKTS_PER_FRAME 个报文拼进一个帧载荷 ----
 *
 * 条目里已经是 [2B 小端长度][IP 报文], 直接首尾相接就是帧载荷格式
 * (与 C5 的 tunnel.c 解析端一致)。放不下的那个条目要**放回**, 不能丢。
 * 返回: 拼好的字节数; *npkt 返回报文个数。
 */
static size_t spitun_tx_pack(u8 *dst, size_t maxlen, int *npkt)
{
    size_t off = 0;
    int n = 0;

    while (n < MAX_PKTS_PER_FRAME) {
        u8 *d = NULL;
        u16 l = 0;

        if (!spitun_dequeue(&d, &l))
            break;
        if (off + l > maxlen) {
            /* 放不下: 放回队尾 (下帧再发), 不丢数据。 */
            if (!spitun_enqueue(d, l))
                kfree(d);
            break;
        }
        memcpy(dst + off, d, l);
        off += l;
        n++;
        kfree(d);
    }

    if (npkt)
        *npkt = n;
    if (n > 1)
        stat_tx_pack++;
    return off;
}

/* ================= 把 IP 报文送上隧道 =================
 *
 * 由 TUN 设备的 ndo_start_xmit 调用, 即内核网络栈要发一个包。
 *
 * ---- 为什么必须入队, 不能在 xmit 里直接发 ----
 *
 * `ndo_start_xmit` 跑在**原子上下文**里 (持有发送锁, 可能还在软中断中),
 * 不能睡眠。而 `spi_sync()` 内部会 `mutex_lock()` 并睡眠。
 *
 * 早期版本就在 xmit 里直接调 `spitun_exchange()`, 结果是内核每次发包
 * 都报:
 *     __schedule_bug
 *     [__mutex_lock] from [spi_sync] from [spitun_exchange]
 *                   from [spitun_ndo_start_xmit]
 * 发送路径被破坏, 回包也收不到 (实测 tx=8 rx=0)。
 *
 * 正确做法: xmit 只做"拷贝 + 入队"(纯原子操作), 真正的 SPI 收发交给
 * spitun_thread 在进程上下文里做 —— 那里可以安全睡眠。
 */
static int spitun_xmit(struct sk_buff *skb, struct net_device *dev)
{
    u8 *frame;
    size_t len = skb->len;

    /* 诊断 (2026-10-07): ip_drop 增长但队列计数不动, 需要看清是
     * "包长非法" 还是 "kmalloc 失败" 还是 "队列满" 在丢。
     * 只在最初几次打印, 避免刷屏。 */
    {
        static int dbg_n;
        if (dbg_n < 8) {
            pr_info(DRV_NAME ": xmit#%d len=%zu mtu=%u\n",
                    dbg_n, len, dev->mtu);
            dbg_n++;
        }
    }

    if (len == 0 || len > 1350) {
        /* 空包或超过隧道 MTU */
        stat_ip_drop++;
        dev->stats.tx_dropped++;
        dev_kfree_skb(skb);
        return NETDEV_TX_OK;
    }

    /* 组一帧: [2B 小端长度][IP 报文] —— 与 C5 的 tunnel.c 布局一致。
     * GFP_ATOMIC: 原子上下文里不能睡眠等内存。 */
    frame = kmalloc(2 + len, GFP_ATOMIC);
    if (!frame) {
        stat_ip_drop++;
        dev->stats.tx_dropped++;
        dev_kfree_skb(skb);
        return NETDEV_TX_OK;
    }

    frame[0] = (u8)(len & 0xFF);
    frame[1] = (u8)((len >> 8) & 0xFF);
    skb_copy_bits(skb, 0, frame + 2, len);

    /* 入队, 由隧道线程真正发送。
     * 队列满就丢 —— TCP 会重传, 比在这里阻塞整个网络栈好。 */
    if (!spitun_enqueue(frame, (u16)(2 + len))) {
        stat_ip_drop++;
        dev->stats.tx_dropped++;
        kfree(frame);
    }

    dev->stats.tx_packets++;
    dev->stats.tx_bytes += len;
    dev_kfree_skb(skb);
    return NETDEV_TX_OK;
}

/* ================= 把收到的 IP 报文注入网络栈 ================= */
static void spitun_rx_ip(const u8 *payload, size_t len)
{
    size_t off = 0;

    while (off + 2 <= len) {
        u16 plen = payload[off] | (payload[off + 1] << 8);
        struct sk_buff *skb;

        off += 2;
        if (plen < 20 || off + plen > len) {
            stat_ip_drop++;
            return;
        }

        skb = dev_alloc_skb(plen + 2);
        if (!skb) {
            stat_ip_drop++;
            return;
        }
        skb_reserve(skb, 2);
        memcpy(skb_put(skb, plen), payload + off, plen);
        skb->dev = tun_netdev;
        skb->ip_summed = CHECKSUM_NONE;

        /* 协议号要直接按 IP 版本判, 不能用 eth_type_trans()。
         *
         * eth_type_trans() 是给**以太网**设备用的: 它把开头 14 字节当
         * 以太网头解析, 取出 ethertype。而 spitun0 是
         * IFF_NOARP|IFF_POINTOPOINT 的裸 IP 设备 —— 载荷第一个字节就是
         * IP 版本号 (0x45), 根本没有以太网头。
         *
         * 用 eth_type_trans() 的后果 (实测):
         *     从 IP 头前 14 字节"解析"出垃圾 ethertype
         *     内核认不出协议 -> 丢弃
         *     /proc/net/dev 显示 rx_packets=0, 但 rx_drop=8
         *     也就是"包到了、但被内核扔了", ping 永远不通。
         */
        if (plen >= 1 && (payload[off] >> 4) == 6)
            skb->protocol = htons(ETH_P_IPV6);
        else
            skb->protocol = htons(ETH_P_IP);

        /* 打印收到的 IP 包头, 定位 "包到了但被丢" 这类问题。
         *
         * 这一步是必须的: InAddrErrors 只会告诉我们"内核不接受这个目的
         * 地址", 但不会说是**什么**地址。而 NAPT 会改写源地址, 很容易
         * 出现"目的地址是局域网地址, 板子不认识所以丢掉"的情况 ——
         * 只看计数器永远查不出来。 */
        if (stat_ip_rx <= 5 || (stat_ip_rx % 100) == 0) {
            if (plen >= 20) {
                const u8 *iph = payload + off;
                pr_info(DRV_NAME ": RX IP %u.%u.%u.%u -> %u.%u.%u.%u "
                        "proto=%u len=%u\n",
                        iph[12], iph[13], iph[14], iph[15],
                        iph[16], iph[17], iph[18], iph[19],
                        iph[9], plen);
            }
        }

        netif_rx(skb);
        stat_ip_rx++;
        off += plen;
    }
}

/* ================= 关于回程路由 (重要) =================
 *
 * 板子有两条通往外界的路:
 *     eth0    192.168.3.80/24   (网线)
 *     spitun0 10.77.0.2/24      (经 C5 到 WiFi)
 *
 * 浏览器从 **WiFi 侧** 访问 (http://192.168.3.69/) 时:
 *     PC -> 路由器 -> C5 -> [SPI] -> 板子
 * 板子回包时查路由表, 看到 192.168.3.0/24 走 eth0 —— **从网线发出去了**。
 * C5 收不到这个回包, 浏览器就一直转圈 (实测 ip route get 192.168.3.64
 * 返回 dev eth0, 就是这个症状)。
 *
 * 旧版用户态隧道用"学习 + /32 主机路由"解决: 从隧道收到某个源 IP 的包,
 * 就给它装一条 /32 指向 spitun0。/32 比 /24 更具体所以优先, 回包自然走隧道。
 *
 * ---- 为什么这个逻辑不在内核里 ----
 *
 * 内核模块要加路由只能用 fib_table_insert(), 但那个符号**没有 EXPORT_SYMBOL**
 * (实测 modpost 报 undefined)。内核导出的只有 rtnl_lock/rtnl_unlock, 拿不到
 * 插入路由的接口。硬做就得自己拼 netlink 消息, 复杂且容易出错。
 *
 * 所以改成由用户态做这件事:
 *   /etc/init.d/S22spinet 在配好接口后, 装一条 192.168.3.0/24 经隧道的路由
 *   (或者针对 PC 的 /32)。这条路由是静态的, 不需要"学习" —— 车就一个
 *   控制端, 地址固定。旧版的动态学习是为了应对多个对端, 这里用不上。
 *
 * 这样内核模块保持简单, 路由策略留在能随时调整的 shell 脚本里。
 */

/* ================= 隧道主循环 ================= */
static int spitun_loop(void *arg)
{
    unsigned long idle = 0;
    unsigned long no_dev = 0;

    pr_info(DRV_NAME ": tunnel thread started\n");

    while (!kthread_should_stop() && !spitun_shutdown) {
        u8 out_type = 0;
        size_t out_len = 0;
        int ret;

        /* 设备还没绑上就等着。
         *
         * 模块由 insmod_ko.sh 加载, 而 spi0.0 的 probe 可能在之后才发生。
         * 早期版本在这里直接调 spi_sync(NULL, ...), 内核直接 oops。
         * 现在退让等待, 并且只在状态变化时报一次, 避免刷屏。 */
        if (!spi_dev) {
            if (no_dev == 0)
                pr_info(DRV_NAME ": waiting for SPI device to bind...\n");
            no_dev++;
            usleep_range(50000, 60000);
            continue;
        }
        if (no_dev) {
            pr_info(DRV_NAME ": SPI device bound after %lu waits\n", no_dev);
            no_dev = 0;
        }

        /* 优先发队列里的 IP 报文, 没有才发空心跳。
         *
         * 为什么心跳也必须发: C5 是 SPI 从机, 它不能主动通知主机。
         * 主机一停, C5 的下行数据就永远送不出来 —— 所以空闲时也要持续轮询。
         *
         * 每帧最多打包 MAX_PKTS_PER_FRAME 个报文 (见 spitun_tx_pack):
         * 一次 exchange 固定 3.1ms / 4096B, 只装一个 1350B 报文是浪费,
         * 也让队列排空慢 3 倍 —— 那正是图传负载下控制延迟的来源。
         *
         * 这里在进程上下文, 可以安全睡眠 (spi_sync 会拿 mutex)。 */
        {
            int npkt = 0;
            int attempt;
            size_t plen = spitun_tx_pack(txpack, MAX_PAYLOAD, &npkt);

            /* 失败立刻重发同一帧 (见 EXCH_RETRY 的说明)。
             *
             * 为什么值得: 这一层丢一帧, 上层 TCP 要等 RTO (>=200ms) 才发现;
             * 而在这里重发只要一帧时间 (~3ms)。帧失败率 burst 时能到 30%,
             * 全靠 TCP 兜的话控制指令就会一顿一顿的。
             *
             * 只对 -EBADMSG (bad magic / 校验错) 重试 —— 那是"从机没武装好"
             * 这类**瞬态**问题。设备没绑定 (-ENODEV) 之类的重试没意义。
             *
             * 而且**必须有预算**: C5 整机不在线时每一帧都会失败, 无脑重试
             * 等于在主循环里空转 (实测 6183 次失败全部白重试 3 遍)。
             * 预算用完就放手, 交给上层 TCP 去管 —— 那种情况本来也救不回来。 */
            for (attempt = 0; attempt < EXCH_RETRY; attempt++) {
                if (plen > 0)
                    ret = spitun_exchange(T_IP, txpack, plen,
                                          &out_type, &out_len);
                else
                    ret = spitun_exchange(T_NODATA, NULL, 0,
                                          &out_type, &out_len);
                if (ret == 0) {
                    if (attempt > 0)
                        stat_retry_ok++;
                    break;
                }
                if (ret != -EBADMSG)
                    break;
                if (!retry_budget_take()) {
                    stat_retry_skip++;
                    break;
                }
                stat_retry++;
                /* 给从机一点时间重新武装。0.1~0.2ms 相对一帧 3ms 可忽略,
                 * 但能把"立刻重试又撞上未武装窗口"的概率压下去。 */
                usleep_range(100, 200);
            }

            if (plen > 0) {
                if (ret == 0)
                    stat_ip_tx += npkt;
                else
                    stat_ip_drop += npkt;
            }
        }

        if (ret == 0) {
            /* 计数器: 先用实测数据定位问题, 不要靠猜。
             *
             * 之前反复出现 "隧道看起来在跑但 IP 不通", 而 log 上没有
             * 任何可用的信息 —— 只能靠改代码重刷固件试探。加了这几个
             * 计数之后, 一眼就能看出是哪一类帧、哪个方向断了。 */
            stat_rx_ok++;
            if (out_type == T_IP)      stat_rx_ip_frames++;
            else if (out_type == T_STAT) stat_rx_stat_frames++;
            else if (out_type == T_HELLO) stat_rx_hello++;
            else                        stat_rx_other++;

            if (out_type == T_IP && out_len > 0) {
                if (stat_rx_ip_frames <= 3 || (stat_rx_ip_frames % 200) == 0)
                    pr_info(DRV_NAME ": RX T_IP frame #%lu len=%zu\n",
                            stat_rx_ip_frames, out_len);
                spitun_rx_ip(rxbuf + HDR_SIZE, out_len);
            }
            else if (out_type == T_STAT && out_len > 0) {
                size_t n = out_len < sizeof(last_stat) - 1
                         ? out_len : sizeof(last_stat) - 1;
                mutex_lock(&stat_lock);
                memcpy(last_stat, rxbuf + HDR_SIZE, n);
                last_stat[n] = 0;
                mutex_unlock(&stat_lock);
            } else if (out_type == T_HELLO) {
                pr_info(DRV_NAME ": HELLO from slave\n");
            }
        } else {
            stat_rx_fail++;
            if (stat_rx_fail <= 5 || (stat_rx_fail % 500) == 0)
                pr_warn(DRV_NAME ": exchange failed rc=%d (#%lu)\n",
                        ret, stat_rx_fail);
        }

        /* 每 30 秒报一次状态, 让日志能自证链路到底在搬什么。
         * 没有这行就只能靠外部 ping 猜, 而 ping 会被 C5 的本地应答
         * 误导 (10.77.0.1 和 192.168.3.69 都是 C5 自己)。 */
        if (time_after(jiffies, stat_next_report)) {
            stat_next_report = jiffies + 30 * HZ;
            /* 拆成两行打: 实测 16 个 %lu 的超长 pr_info 在这块板子的
             * ring buffer 里一次都没出现过 (短行正常), 拆开更稳。 */
            pr_info(DRV_NAME ": frames=%lu ok=%lu fail=%lu bad=%lu retry=%lu "
                    "retry_ok=%lu\n",
                    stat_tx_frames, stat_rx_ok, stat_rx_fail,
                    stat_bad_magic, stat_retry, stat_retry_ok);
            pr_info(DRV_NAME ": ip tx=%lu rx=%lu drop=%lu | hi %lu/%lu | "
                    "multipack=%lu\n",
                    stat_ip_tx, stat_ip_rx, stat_ip_drop,
                    stat_hi_enq, stat_hi_drop, stat_tx_pack);
        }

        stat_tx_frames++;

        /* 全空时让出 CPU。
         *
         * 1ms: 单核板子上 rkipc/mediamtx 同样需要时间片。实测一次 exchange
         * 要 3.1ms, 所以这 1ms 只增加约 1/3 帧的延迟, 换取的是编码线程
         * 不被饿死。
         */
        if (out_type == T_NODATA && out_len == 0) {
            if (++idle >= 2) {
                usleep_range(1000, 1500);
                idle = 0;
            }
        } else {
            idle = 0;
        }
    }

    pr_info(DRV_NAME ": tunnel thread stopped\n");
    return 0;
}

/* ================= TUN 网络设备 ================= */

static netdev_tx_t spitun_ndo_start_xmit(struct sk_buff *skb,
                                         struct net_device *dev)
{
    return spitun_xmit(skb, dev);
}

static const struct net_device_ops spitun_netdev_ops = {
    .ndo_start_xmit = spitun_ndo_start_xmit,
};

static void spitun_setup_netdev(struct net_device *dev)
{
    dev->netdev_ops  = &spitun_netdev_ops;
    dev->flags      |= IFF_NOARP | IFF_POINTOPOINT;
    dev->features   |= NETIF_F_NETNS_LOCAL;
    dev->mtu         = 1350;   /* 3 个包正好塞进一帧: 3*1350 + 3*2 = 4056 */
    dev->hard_header_len = 0;
    dev->addr_len        = 0;
    dev->tx_queue_len    = 500;

    /* ---- dev->type 必须设置 (2026-10-08 实测踩的坑) ----
     *
     * 原来这里**没有**设置 dev->type, 于是它是 0 = ARPHRD_VOID。
     * 实测后果 (刷机后第一次把模块真正跑起来时暴露):
     *
     *   /sys/class/net/spitun0/type      -> 0        <-- 应该是 ARPHRD_NONE
     *   tx_packets=0 tx_bytes=0 tx_dropped=36
     *   rx_packets=0 rx_bytes=0 rx_dropped=10
     *   dmesg: spitun: xmit#N len=0 mtu=1350        <-- 内核给的是空 skb
     *   /proc/net/snmp: InAddrErrors 持续增长        <-- 注入的包被内核拒绝
     *
     * 即 **TX 和 RX 双向全废**, 但 SPI 链路本身是好的
     * (frames/ok 一直在涨, fail=0 bad_magic=0)。
     *
     * 对照: 用户态 spinet_c 用的是**真 TUN 设备**
     * (IFF_TUN|IFF_NO_PI, 由内核 tun 驱动配好 netdev), 所以没这个问题。
     * 我们自己 alloc_netdev 就必须把 netdev 语义配全。
     *
     * ARPHRD_NONE (0xFFFE) 是"没有链路层地址的裸 IP 设备", 正是
     * IFF_NOARP|IFF_POINTOPOINT 隧道该有的 type。
     *
     * 参考 lo=772(ARPHRD_LOOPBACK), eth0=1(ARPHRD_ETHER)。 */
    dev->type            = ARPHRD_NONE;

    /* 挂上 sysfs 属性组: /sys/class/net/spitun0/c3_status
     * 网页靠它显示 C5 的 WiFi 信号 (见 c3_status_show 的说明)。
     *
     * sysfs_groups[] 每个槽位是单个指针 (const struct attribute_group *),
     * 所以取地址 &spitun_group。 */
    dev->sysfs_groups[0] = &spitun_group;
}

/* ================= 模块初始化 ================= */

static int spitun_probe(struct spi_device *spi)
{
    int ret;

    spi_dev = spi;
    spi->mode = SPI_MODE_0;
    spi->bits_per_word = 8;
    spi->max_speed_hz = spi_speed_hz;

    ret = spi_setup(spi);
    if (ret < 0) {
        pr_err(DRV_NAME ": spi_setup failed: %d\n", ret);
        return ret;
    }

    pr_info(DRV_NAME ": SPI ready: bus=%d cs=%d speed=%u Hz\n",
            spi->master->bus_num, spi->chip_select, spi->max_speed_hz);

    return 0;
}

static const struct spi_device_id spitun_ids[] = {
    { "spitun", 0 },
    { }
};
MODULE_DEVICE_TABLE(spi, spitun_ids);

/* 设备树匹配表。
 *
 * 没有这张表, 内核不会把设备树里的 spitun@0 节点绑到本驱动上 ——
 * 驱动注册了却永远收不到 probe, 表现为:
 *     /sys/bus/spi/drivers/spitun/  空目录
 *     dmesg 里没有 "SPI ready"
 * 实测就是这样: 模块加载成功、spitun0 建好了, 但 spi_dev 是 NULL,
 * 隧道线程拿不到总线可用。
 *
 * compatible 字符串必须与 dts 里的 "spitun" 完全一致。 */
static const struct of_device_id spitun_of_match[] = {
    { .compatible = "spitun" },
    { }
};
MODULE_DEVICE_TABLE(of, spitun_of_match);

/* 用 of_device_id 匹配设备树里的 spidev 节点。
 *
 * 注意: 板子的 /dev/spidev0.0 是 spidev 驱动占着的, 我们的驱动不能直接
 * 抢它 —— 但作为模块验证, 先注册驱动看能否 probe 成功; 真正的部署需要
 * 改设备树, 或者在 probe 里拿到 spi_device 后自己管总线。
 *
 * 当前阶段的目标是验证"模块能加载", 所以即使不 probe 也算成功。 */
static struct spi_driver spitun_spi_driver = {
    .driver = {
        .name = DRV_NAME,
        .of_match_table = spitun_of_match,
    },
    .id_table = spitun_ids,
    .probe    = spitun_probe,
};

/* module_spi_driver() 会生成 init/exit, 所以不能再写 module_init/module_exit,
 * 否则 __inittest/init_module 会重复定义 (踩过这个编译错误)。
 *
 * 但我们的隧道线程需要在**模块加载时**就启动, 而不是等 SPI 设备 probe ——
 * 因为 spidev 已经占了总线, 我们可能永远收不到 probe。
 * 所以这里不用 module_spi_driver, 而是手写 init/exit, 在里面:
 *   1. 注册 SPI 驱动 (可选, 为了拿 spi_device)
 *   2. 起 TUN 网络设备
 *   3. 起隧道线程
 */
static int __init spitun_init(void)
{
    int ret;

    pr_info(DRV_NAME ": loading (frame=%d, hdr=%d)\n",
            FRAME_SIZE, HDR_SIZE);

    /* DMA 缓冲: kmalloc 保证物理连续, 4096 字节远小于一页 */
    txbuf = kmalloc(FRAME_SIZE, GFP_KERNEL | GFP_DMA);
    rxbuf = kmalloc(FRAME_SIZE, GFP_KERNEL | GFP_DMA);
    if (!txbuf || !rxbuf) {
        pr_err(DRV_NAME ": cannot allocate DMA buffers\n");
        ret = -ENOMEM;
        goto err_free;
    }
    memset(txbuf, 0, FRAME_SIZE);
    memset(rxbuf, 0, FRAME_SIZE);

    /* 注册 TUN 风格的网络设备 */
    tun_netdev = alloc_netdev(0, "spitun0", NET_NAME_UNKNOWN,
                              spitun_setup_netdev);
    if (!tun_netdev) {
        pr_err(DRV_NAME ": alloc_netdev failed\n");
        ret = -ENOMEM;
        goto err_free;
    }

    ret = register_netdev(tun_netdev);
    if (ret < 0) {
        pr_err(DRV_NAME ": register_netdev failed: %d\n", ret);
        goto err_uninit;
    }

    pr_info(DRV_NAME ": netdev %s registered\n", tun_netdev->name);

    /* 注册 SPI 驱动。失败不致命 —— 总线可能被 spidev 占着,
     * 那只是说明我们拿不到 spi_device, 不影响模块加载本身。 */
    ret = spi_register_driver(&spitun_spi_driver);
    if (ret < 0) {
        pr_warn(DRV_NAME ": spi_register_driver failed: %d "
                "(bus likely owned by spidev; module still loads)\n", ret);
    } else {
        pr_info(DRV_NAME ": spi driver registered\n");
    }

    /* 隧道线程只在拿到 spi_device 后才有意义。
     * 先启动, 线程里检查 spi_dev 是否为空。 */
    spitun_thread = kthread_run(spitun_loop, NULL, "spitun");
    if (IS_ERR(spitun_thread)) {
        ret = PTR_ERR(spitun_thread);
        pr_err(DRV_NAME ": cannot create thread: %d\n", ret);
        spitun_thread = NULL;
        goto err_spi;
    }

    pr_info(DRV_NAME ": loaded OK (tunnel thread starts after SPI binds)\n");
    return 0;

err_spi:
    spi_unregister_driver(&spitun_spi_driver);
    unregister_netdev(tun_netdev);
err_uninit:
    free_netdev(tun_netdev);
    tun_netdev = NULL;
err_free:
    kfree(txbuf);
    kfree(rxbuf);
    txbuf = rxbuf = NULL;
    return ret;
}

static void __exit spitun_exit(void)
{
    pr_info(DRV_NAME ": unloading\n");

    spitun_shutdown = true;

    if (spitun_thread) {
        kthread_stop(spitun_thread);
        spitun_thread = NULL;
    }

    /* 队列里可能还有没发出去的报文, 必须释放 —— 否则每次
     * rmmod/insmod 都泄漏几十 KB。 */
    spitun_txq_flush();

    spi_unregister_driver(&spitun_spi_driver);

    if (tun_netdev) {
        unregister_netdev(tun_netdev);
        free_netdev(tun_netdev);
        tun_netdev = NULL;
    }

    kfree(txbuf);
    kfree(rxbuf);
    txbuf = rxbuf = NULL;

    pr_info(DRV_NAME ": unloaded (tx=%lu rx=%lu err=%lu ip_tx=%lu ip_rx=%lu drop=%lu)\n",
            stat_tx_frames, stat_rx_frames, stat_errors,
            stat_ip_tx, stat_ip_rx, stat_ip_drop);
}

module_init(spitun_init);
module_exit(spitun_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("luckfox car project");
MODULE_DESCRIPTION("SPI tunnel: TUN over SPI to an ESP32-C5 (kernel implementation)");
MODULE_VERSION("0.1");

#!/bin/sh
# spitun 模块热替换 + 自动回滚
#
# 背景: spitun.ko 是远程通道本身。rmmod 的一瞬间, 板子与 PC 之间的
# SSH/网页通道就断了 —— 所以这个脚本必须**脱离 SSH 会话**运行
# (setsid nohup), 自己完成"加载 -> 验证 -> 失败则回滚"的全过程。
#
# 判据用 C5 每秒推的状态帧 up_ms 是否在涨 (S22spinet 里的 tunnel_alive):
# 它直接证明 SPI 双向都在工作, 比 ping 可靠 (10.77.0.1 永远 ping 不通, 见 S22spinet)。
#
# 兜底: 万一新旧模块都起不来, spitun0 消失后那条 192.168.3.64/32 回程路由
# 也随之消失, 网线通道 (eth0 192.168.3.84) 会自动接客 —— 不会把自己锁死。

LOG=/userdata/spitun_reload.log
NEW=/userdata/spitun_new.ko
OLD=/userdata/spitun_prev.ko
OEM=/oem/usr/ko/spitun.ko

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

tunnel_alive() {
    A=$(sed 's/.*"up_ms":\([0-9]*\).*/\1/' /sys/class/net/spitun0/c3_status 2>/dev/null)
    [ -n "$A" ] || return 1
    sleep 1
    B=$(sed 's/.*"up_ms":\([0-9]*\).*/\1/' /sys/class/net/spitun0/c3_status 2>/dev/null)
    [ -n "$B" ] || return 1
    [ "$A" != "$B" ]
}

# 接口 + 地址 + 数据面, 三条都过才算"可用"
verify() {
    ip link show spitun0 >/dev/null 2>&1 || { log "  verify: 没有 spitun0"; return 1; }
    ip addr show spitun0 2>/dev/null | grep -q '10.77.0.2' || { log "  verify: 地址缺失"; return 1; }
    tunnel_alive || { log "  verify: c3_status 不更新"; return 1; }
    return 0
}

load_and_config() {
    KO="$1"
    log "  rmmod spitun"
    rmmod spitun >> "$LOG" 2>&1
    sleep 1
    log "  insmod $KO"
    insmod "$KO" >> "$LOG" 2>&1
    RC=$?
    log "  insmod rc=$RC"
    [ $RC -ne 0 ] && return 1
    sleep 2
    log "  S22spinet restart (配地址 + 回程路由)"
    /etc/init.d/S22spinet restart >> "$LOG" 2>&1
    return 0
}

log "================ 开始热替换 ================"
log "新模块: $(md5sum $NEW 2>/dev/null)"
log "旧模块: $(md5sum $OLD 2>/dev/null)"

# 1) 新模块
if load_and_config "$NEW" && verify; then
    log "结果: 新模块加载成功且隧道可用"
    echo "NEW_OK"
    exit 0
fi

# 2) 回滚
log "!! 新模块不可用 -> 回滚到旧模块"
if load_and_config "$OLD" && verify; then
    log "结果: 已回滚, 隧道恢复 (旧模块)"
    echo "ROLLED_BACK"
    exit 1
fi

log "结果: !! 新旧模块都不可用, 隧道离线 (网线通道 eth0 仍可用)"
echo "BOTH_FAILED"
exit 2

#!/bin/sh
# deploy_all.sh -- 把小车所有的板载服务脚本部署到板子并启动。
#
# ============================================================================
# 为什么需要这个脚本
# ============================================================================
# /etc/init.d 里的脚本**重启保留, 但刷固件会全部丢失**。本轮就因为刷了一次
# 固件, 导致:
#   * udhcpc 守卫没了   -> 隧道地址被清空, WiFi 控制整条断掉
#   * lo 没有 127.0.0.1 -> mediamtx 连不上 rkipc, 视频不出来
#   * mediamtx 没有 init 脚本 -> 重启后从来没启动过
# 每一项都很隐蔽, 排查代价极大。所以刷完机必须跑一次本脚本。
#
# 用法: 把 car/ 下这几个文件 scp 到 /tmp/ 后, 在板子上 sh /tmp/deploy_all.sh
# ============================================================================

set -e

echo "=========================================="
echo " 部署板载服务脚本"
echo "=========================================="

# ---- 1) 安装 init 脚本 ----
for f in S20lo S21wdt S22spinet S23web S24spinet_wd S25mediamtx; do
    if [ -f "/tmp/$f" ]; then
        cp -f "/tmp/$f" "/etc/init.d/$f"
        chmod 755 "/etc/init.d/$f"
        echo "  [OK] /etc/init.d/$f"
    else
        echo "  [跳过] /tmp/$f 不存在"
    fi
done

# 注意: 硬件看门狗 (S21wdt) 依赖**设备树里 &wdt 已启用**, 也就是
# /dev/watchdog 必须存在。刷机时如果用的是没有改过设备树的内核,
# 这一步会明确报错 (脚本会打印 "/dev/watchdog 不存在")。
# 本轮已经把 &wdt 编进了内核, 所以刷机后应当直接可用。

# ---- 2) 按依赖顺序启动 ----
echo ""
echo "--- 启动 S20lo (回环 127.0.0.1) ---"
/etc/init.d/S20lo restart 2>&1 || true

echo "--- 启动 S21wdt (硬件看门狗) ---"
/etc/init.d/S21wdt restart 2>&1 || true

echo "--- 启动 S22spinet (SPI 隧道) ---"
/etc/init.d/S22spinet restart 2>&1 | tail -5 || true

echo "--- 启动 S23web (网页控制) ---"
/etc/init.d/S23web restart 2>&1 || true

echo "--- 启动 S24spinet_wd (隧道看门狗) ---"
/etc/init.d/S24spinet_wd restart 2>&1 || true

echo "--- 启动 S25mediamtx (图传) ---"
/etc/init.d/S25mediamtx restart 2>&1 | tail -5 || true

# ---- 3) 汇总 ----
echo ""
echo "=========================================="
echo " 部署完成, 当前状态:"
echo "=========================================="

echo -n "  lo 127.0.0.1 : "
ip addr show lo | grep -q '127\.0\.0\.1' && echo "OK" || echo "缺失!"

echo -n "  硬件看门狗   : "
if [ -e /dev/watchdog ]; then
    /etc/init.d/S21wdt status 2>/dev/null | head -1 | sed 's/^/  /'
else
    echo "!! /dev/watchdog 不存在 (内核没启用 &wdt, 卡死无法自动复位!)"
fi

echo -n "  spitun0 地址 : "
ip addr show spitun0 2>/dev/null | grep 'inet ' || echo "缺失!"

echo -n "  udhcpc 守卫  : "
grep -q 'spitun0|tun0' /usr/share/udhcpc/default.script 2>/dev/null && echo "OK" || echo "缺失!"

echo -n "  C5 状态      : "
cat /sys/class/net/spitun0/c3_status 2>/dev/null | head -c 120
echo ""

echo "  监听端口:"
netstat -tln 2>/dev/null | grep -E ':80 |:554 |:8889|:8189|:8888' | sed 's/^/    /'

echo ""
echo "  进程:"
ps | grep -E '[w]eb_server|[m]ediamtx|[r]kipc' | sed 's/^/    /'

echo ""
echo "  隧道诊断 (bad_magic 是否增长):"
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]*' | tail -1 | sed 's/^/    /'

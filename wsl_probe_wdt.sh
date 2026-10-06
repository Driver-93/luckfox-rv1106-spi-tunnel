#!/bin/bash
cd /root/sdk/luckfox-pico-main/sysdrv/source/kernel || exit 1
echo "=== rv1106 defconfig 文件 ==="
ls arch/arm/configs/ | grep -i 1106
echo ""
echo "=== 每个 defconfig 里的 WATCHDOG 配置 ==="
for f in arch/arm/configs/*1106*; do
    echo "--- $f ---"
    grep -E 'WATCHDOG' "$f" || echo "  (无 WATCHDOG 项)"
done
echo ""
echo "=== 当前是否已有编译好的 .config ==="
if [ -f .config ]; then
    echo "  有 .config"
    grep -E 'CONFIG_(DW_WATCHDOG|WATCHDOG_CORE|WATCHDOG_NOWAYOUT)' .config || echo "  .config 里没有 watchdog 项"
else
    echo "  没有 .config (还没编译过)"
fi
echo ""
echo "=== dw_wdt 驱动源码存在性 ==="
ls -l drivers/watchdog/dw_wdt.c
echo ""
echo "=== watchdog 目录里的 rockchip 相关 ==="
ls drivers/watchdog/ | head -30

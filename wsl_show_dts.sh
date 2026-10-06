#!/bin/bash
DTS=/root/sdk/luckfox-pico-main/sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-luckfox-pico-pro-max.dts
echo "=== $DTS ==="
echo "总行数: $(wc -l < $DTS)"
echo ""
echo "=== 文件末尾 60 行 (我之前加的 spi0/uart4 在这里) ==="
tail -60 "$DTS"
echo ""
echo "=== 是否已有 wdt 引用 ==="
grep -n 'wdt' "$DTS" || echo "  (没有)"

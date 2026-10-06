#!/bin/bash
K=/root/sdk/luckfox-pico-main/sysdrv/source/kernel
echo "=== spi-rockchip.c 530-600 (带超时的等待) ==="
sed -n '530,600p' "$K/drivers/spi/spi-rockchip.c"
echo ""
echo "=== 700-780 (DMA 完成判定) ==="
sed -n '700,780p' "$K/drivers/spi/spi-rockchip.c"
echo ""
echo "=== 220-260 (那个 5ms 超时的函数) ==="
sed -n '220,260p' "$K/drivers/spi/spi-rockchip.c"

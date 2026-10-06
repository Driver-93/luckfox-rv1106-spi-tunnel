#!/bin/bash
K=/root/sdk/luckfox-pico-main/sysdrv/source/kernel
cd "$K" || exit 1

echo "=== SPI 驱动文件 ==="
ls -l drivers/spi/spi-rockchip.c 2>/dev/null

echo ""
echo "=== 驱动里的超时相关 ==="
grep -nE 'timeout|TIMEOUT|wait_for_completion|completion_timeout|msecs_to_jiffies' \
    drivers/spi/spi-rockchip.c | head -30

echo ""
echo "=== spi_transfer_one_message 里的等待 (核心) ==="
grep -n -A25 'static int rockchip_spi_transfer_one' drivers/spi/spi-rockchip.c | head -40

echo ""
echo "=== 传输等待函数 rockchip_spi_wait_for_transfer / wait ==="
grep -n -B2 -A30 'wait_for_transfer\|rockchip_spi_wait' drivers/spi/spi-rockchip.c | head -60

echo ""
echo "=== 是否有 DMA 模式 (DMA 死锁是常见原因) ==="
grep -nE 'use_dma|dma_request|dmaengine' drivers/spi/spi-rockchip.c | head -20

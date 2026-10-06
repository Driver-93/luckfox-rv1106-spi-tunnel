#!/bin/sh
echo "--- 精确读回 3724800 字节 ---"
dd if=/dev/mtd3 of=/tmp/boot_exact.img bs=3724800 count=1 2>/dev/null
echo "读回大小: $(wc -c < /tmp/boot_exact.img | tr -d ' ')"

echo ""
echo "--- md5 对比 ---"
A=$(md5sum /tmp/boot_wdt.img | cut -d' ' -f1)
B=$(md5sum /tmp/boot_exact.img | cut -d' ' -f1)
echo "  源   : $A"
echo "  读回 : $B"
if [ "$A" = "$B" ]; then
    echo "  ✅ 完全一致, 可以安全重启"
else
    echo "  ❌ 不一致! 不要重启, 先恢复:"
    echo "     flash_erase /dev/mtd3 0 0 && nandwrite -p /dev/mtd3 /tmp/boot_OLD.img"
fi

echo ""
echo "--- 镜像头 (应为 Rockchip FIT/资源镜像) ---"
dd if=/dev/mtd3 bs=32 count=1 2>/dev/null | od -A x -t x1z | head -3

echo ""
echo "--- 对比旧备份的头 ---"
dd if=/tmp/boot_OLD.img bs=32 count=1 2>/dev/null | od -A x -t x1z | head -3

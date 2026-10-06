#!/bin/bash
# 重建内核 (带硬件看门狗的设备树)。
# 注意: 必须过滤掉 PATH 里的 /mnt/* 条目, 否则 buildroot 会因为
# Windows 路径而拒绝构建 (老问题)。
cd /root/sdk/luckfox-pico-main || exit 1

export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | paste -sd:)

echo "=== PATH 过滤后 ==="
echo "$PATH" | tr ':' '\n' | head -8
echo ""

echo "=== 确认 dts 里有 &wdt ==="
grep -c '^&wdt' sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-luckfox-pico-pro-max.dts

echo ""
echo "=== 开始构建内核 ==="
./build.sh kernel 2>&1 | tail -50

echo ""
echo "=== 构建结果 ==="
find sysdrv/source/kernel/arch/arm/boot -name 'rv1106g-luckfox-pico-pro-max.dtb' 2>/dev/null
ls -l sysdrv/source/kernel/arch/arm/boot/zImage 2>/dev/null

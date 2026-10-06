#!/bin/bash
cd /root/sdk/luckfox-pico-main || exit 1
echo "=== SDK 顶层 ==="
ls
echo ""
echo "=== 是否有已编译的内核 dtb ==="
find sysdrv/source/kernel/arch/arm/boot -name '*.dtb' 2>/dev/null | head -10
echo ""
echo "=== 内核是否已配置 (.config) ==="
ls -l sysdrv/source/kernel/.config 2>/dev/null || echo "  没有 .config"
echo ""
echo "=== output 目录 (上次构建产物) ==="
ls -l output/ 2>/dev/null | head
echo ""
echo "=== 上次的 update.img ==="
find . -name 'update.img' 2>/dev/null | head -5
echo ""
echo "=== build.sh 支持哪些 target ==="
grep -nE '^\s+(kernel|all|firmware|rootfs|uboot|clean|help)\)' build.sh 2>/dev/null | head -20
echo "--- build.sh 用法说明 ---"
grep -n -A5 'Usage\|usage' build.sh 2>/dev/null | head -30

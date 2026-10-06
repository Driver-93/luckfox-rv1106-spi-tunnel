#!/bin/bash
cd /root/sdk/luckfox-pico-main || exit 1
echo "=== config 目录 (板级配置列表) ==="
ls config/ | head -40
echo ""
echo "=== 上次构建留下的配置线索 ==="
ls -la output/out/ 2>/dev/null | head -20
echo ""
echo "=== 有没有保存的 .config / .BoardConfig ==="
find . -maxdepth 3 -name '.BoardConfig*' -o -maxdepth 3 -name '.config' 2>/dev/null | head
echo ""
echo "=== build.sh lunch 的可选项 (从脚本里提取) ==="
grep -n 'RK_KERNEL_DTS\|BoardConfig' build.sh 2>/dev/null | head -20
echo ""
echo "=== 环境里是否已有 RK_ 变量 ==="
env | grep '^RK_' || echo "  (无RK_变量)"
echo ""
echo "=== 是不是有项目级配置存了 lunch 选择 ==="
ls -la output/out/*.mk output/out/.config 2>/dev/null
cat output/out/.config 2>/dev/null | head -30

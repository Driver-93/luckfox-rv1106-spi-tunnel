#!/bin/bash
# 交叉编译 rknn_probe.c
# 用 SDK 的 uclibc 工具链 (与板子 libc 一致) + SDK 里的 rknn_api.h
set -e

REPO=/mnt/c/Users/pcX/Documents/luckfox-flash
SDK=/root/sdk/luckfox-pico-main
TC=$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin
HDR=$SDK/project/app/rk_smart_door/smart_door/common/face/algo
B=/root/npu_build

export PATH="$TC:$PATH"
CC=arm-rockchip830-linux-uclibcgnueabihf-gcc

mkdir -p $B
cp -f "$REPO/_npu_src/rknn_probe.c" $B/

echo "=== 编译器 ==="
$CC --version | head -1

echo
echo "=== 头文件存在? ==="
ls -l "$HDR/rknn_api.h"

echo
echo "=== 编译 ==="
cd $B
# librknnmrt 只在运行时链接; 这里用 -l 指向 SDK 里的那份
$CC -O2 -Wall -o rknn_probe rknn_probe.c \
    -I"$HDR" \
    -L"$SDK/output/out/oem/usr/lib" -lrknnmrt 2>&1 | tail -20 || {
        echo "--- 加 -lrknnmrt 失败, 试不链接库 (只做语法/结构体验证) ---"
        $CC -O2 -Wall -o rknn_probe rknn_probe.c -I"$HDR" 2>&1 | tail -20
    }

echo
echo "=== 产物 ==="
ls -l $B/rknn_probe
file $B/rknn_probe 2>/dev/null || true
echo
echo "=== ELF 信息 (确认 uclibc / armv7) ==="
$TC/arm-rockchip830-linux-uclibcgnueabihf-readelf -h $B/rknn_probe 2>/dev/null | grep -E 'Class|Machine|Flags' || true
echo
echo "=== 依赖的库 ==="
$TC/arm-rockchip830-linux-uclibcgnueabihf-readelf -d $B/rknn_probe 2>/dev/null | grep NEEDED || true

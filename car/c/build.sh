#!/bin/sh
# 编译 carctl.c -> carctl (Luckfox 交叉编译)
# 用法:
#   ./build.sh                # 用默认编译器(若在 Luckfox SDK 环境)
#   CC=arm-linux-gnueabihf-gcc ./build.sh   # 指定交叉编译器
#
# 需要 ARM 交叉工具链。常见来源:
#   - Luckfox SDK:  SDK源码/buildroot/output/host/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc
#   - Linaro:       arm-linux-gnueabihf-gcc (ARMv7, glibc)
#   - musl:         arm-linux-musleabihf-gcc (更小, 推荐给64MB板)
#
# 建议: RV1103 用 musl 工具链编译, 体积小省内存。

CC="${CC:-arm-linux-gnueabihf-gcc}"

echo "编译器: $CC"
if ! command -v "$CC" >/dev/null 2>&1; then
    echo "❌ 找不到 $CC"
    echo "   请先安装/指定交叉编译器, 或用 Luckfox SDK 的 gcc。"
    echo "   例如: CC=/path/to/arm-linux-gnueabihf-gcc sh build.sh"
    exit 1
fi

$CC -O2 -s -Wall -Wextra -o carctl carctl.c -lpthread || exit 1
echo "✅ 已生成: carctl"
file carctl
# 可选: 放到板子
# scp carctl root@<板IP>:/userdata/car/

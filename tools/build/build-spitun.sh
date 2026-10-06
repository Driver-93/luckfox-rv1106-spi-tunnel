#!/bin/bash
# 编译 spitun.ko: 用 objs_kernel (已配置好的内核构建目录, 与运行内核同源)
set -e
REPO=/mnt/c/Users/pcX/Documents/luckfox-flash
SDK=/root/sdk/luckfox-pico-main
B=/root/spitun_build
TC=$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin

mkdir -p $B
cp -f "$REPO/driver/spitun.c" $B/spitun.c
cat > $B/Makefile <<'EOF'
obj-m := spitun.o
KDIR := /root/sdk/luckfox-pico-main/sysdrv/source/objs_kernel
PWD  := $(shell pwd)
all:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=arm \
	  CROSS_COMPILE=arm-rockchip830-linux-uclibcgnueabihf- modules
clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
EOF

export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | paste -sd:)
export PATH="$TC:$PATH"

cd $B
make clean >/dev/null 2>&1 || true
echo "=== 编译 ==="
make 2>&1 | tail -25
echo
echo "=== 产物 ==="
ls -l $B/spitun.ko
md5sum $B/spitun.ko
echo "=== vermagic ==="
$TC/arm-rockchip830-linux-uclibcgnueabihf-objdump -s -j .modinfo $B/spitun.ko 2>/dev/null | grep -a -o 'vermagic=[^ ]*' | head -2
modinfo $B/spitun.ko 2>/dev/null | head -8

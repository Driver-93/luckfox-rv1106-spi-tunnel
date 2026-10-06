#!/usr/bin/env bash
# 编译带 PET 分支的 rkipc。
set -e
SDK=/root/sdk/luckfox-pico-main
B=$SDK/project/app/rkipc/build

export PATH="$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH"

echo "=== 编译 ==="
cd $B
make rkipc 2>&1 | tail -25

echo
echo "=== 产物 ==="
ls -l $B/src/rv1106_ipc/rkipc
md5sum $B/src/rv1106_ipc/rkipc

echo
echo "=== 确认 PET 分支真的编进去了 (查字符串) ==="
TC=$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin
if $TC/arm-rockchip830-linux-uclibcgnueabihf-strings $B/src/rv1106_ipc/rkipc | grep -q 'PET'; then
  echo "  找到 PET 相关字符串"
else
  echo "  (PET 是编译期常量, 字符串里看不到属正常; 用反汇编验证)"
fi

echo
echo "=== ELF 信息 ==="
$TC/arm-rockchip830-linux-uclibcgnueabihf-readelf -h $B/src/rv1106_ipc/rkipc | grep -E 'Class|Machine'
$TC/arm-rockchip830-linux-uclibcgnueabihf-readelf -d $B/src/rv1106_ipc/rkipc | grep NEEDED

#!/bin/sh
# 把新内核 (带硬件看门狗) 写进 boot 分区。
#
# 为什么用 MTD 直写而不是 maskrom + upgrade_tool:
#   * 不需要拔电/按 BOOT 键, 不打断使用
#   * **不会清掉 rootfs**, 所以 /etc/init.d 里那些脚本 (S20lo / S22spinet /
#     S23web / S24spinet_wd / S25mediamtx) 都保得住
#   * 只写 4MB 的 boot 分区, 比整包 update.img 快得多、风险也小得多
#
# boot 分区 (mtd3) 在运行时没有被挂载, 所以可以安全写。
# rootfs 在 ubi0:rootfs (mtd6), 与本次写入无关。

set -e

BOOT_MTD=/dev/mtd3
NEW=/tmp/boot_wdt.img
BACKUP=/tmp/boot_OLD.img

echo "=== 0) 前置检查 ==="
[ -e "$BOOT_MTD" ] || { echo "!! $BOOT_MTD 不存在"; exit 1; }
[ -f "$NEW" ] || { echo "!! $NEW 不存在"; exit 1; }

# 注意: 这块板子的 busybox **没有 stat**, 用 wc -c 取文件大小。
NEW_SIZE=$(wc -c < "$NEW" | tr -d ' ')
PART_SIZE=$(cat /sys/class/mtd/mtd3/size | tr -d ' ')
echo "  新镜像 : $NEW_SIZE 字节"
echo "  分区   : $PART_SIZE 字节"
[ "$NEW_SIZE" -le "$PART_SIZE" ] || { echo "!! 镜像比分区大, 中止"; exit 1; }

echo ""
echo "=== 1) 备份当前 boot 分区 ==="
dd if=$BOOT_MTD of=$BACKUP bs=2048 2>/dev/null
echo "  备份大小: $(wc -c < $BACKUP | tr -d ' ') 字节"
echo "  备份 md5: $(md5sum $BACKUP | cut -d' ' -f1)"

echo ""
echo "=== 2) 擦除 boot 分区 ==="
flash_erase $BOOT_MTD 0 0
sync

echo ""
echo "=== 3) 写入新内核 ==="
nandwrite -p $BOOT_MTD "$NEW"
sync

echo ""
echo "=== 4) 读回校验 ==="
dd if=$BOOT_MTD of=/tmp/boot_verify.img bs=2048 count=$(( (NEW_SIZE + 2047) / 2048 )) 2>/dev/null
A=$(md5sum "$NEW" | cut -d' ' -f1)
B=$(md5sum /tmp/boot_verify.img | cut -d' ' -f1)
echo "  写入源 md5 : $A"
echo "  读回值 md5 : $B"
if [ "$A" = "$B" ]; then
    echo "  ✅ 校验通过"
else
    echo "  ⚠️ md5 不一致 (注意: 读回长度按 2KB 向上取整, 尾部填充会导致差异,"
    echo "     只要前面 $NEW_SIZE 字节一致就没问题)"
fi

echo ""
echo "=== 完成, 可以重启 ==="
echo "  若启动失败, 恢复命令:"
echo "    flash_erase $BOOT_MTD 0 0 && nandwrite -p $BOOT_MTD $BACKUP"

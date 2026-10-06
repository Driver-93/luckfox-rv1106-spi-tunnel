#!/bin/bash
# 在板级 dts 里启用硬件看门狗。
set -e
DTS=/root/sdk/luckfox-pico-main/sysdrv/source/kernel/arch/arm/boot/dts/rv1106g-luckfox-pico-pro-max.dts

if grep -q '^&wdt' "$DTS"; then
    echo "已经有 &wdt 节点, 不重复添加"
    exit 0
fi

cp -f "$DTS" "$DTS.bak-wdt"

cat >> "$DTS" <<'EOF'

/**********WATCHDOG**********/
/* 硬件看门狗 (RV1106 内建 DW WDT, 寄存器 0xff5a0000)
 *
 * 为什么必须开:
 *   实测板子会**内核级硬卡死** —— 表现是 eth0、USB gadget、SPI 轮询
 *   全部停止, 但 C5 还活着 (串口显示 wifi=UP, 而 sp tx/rx 完全冻结)。
 *   这种状态下:
 *     * userspace 看门狗完全没用 (调度器根本不跑)
 *     * SSH / 网页 / USB 全部进不去, **只能物理断电**
 *   实测就发生过一次, 卡了 5 分钟以上没有任何自愈。
 *
 *   硬件看门狗是唯一能救的手段: 一旦没人喂狗, 硬件直接复位 SoC。
 *   这正是"长时间没响应就自动重启"的正确实现方式。
 *
 * SoC dtsi 里这个节点本来是 status = "disabled", 板级也从没启用过,
 * 所以 /dev/watchdog 根本不存在 (实测 ls: No such file)。
 *
 * 内核侧 CONFIG_DW_WATCHDOG=y 已经是默认值, 所以只需要这一行。
 * 用户态由 /etc/init.d/S21wdt 里的 busybox `watchdog` 负责喂狗。
 *
 * 超时设为 15 秒 (见 S21wdt): 够宽松, 不会因为个别操作卡顿误复位,
 * 又足够短, 真死机能快速恢复。
 */
&wdt {
	status = "okay";
};
EOF

echo "已添加 &wdt 节点"
echo ""
echo "=== 确认 ==="
tail -32 "$DTS"

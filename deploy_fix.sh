#!/bin/sh
chmod 755 /etc/init.d/S22spinet /etc/init.d/S24spinet_wd
echo "===== 重启隧道 ====="
/etc/init.d/S22spinet restart 2>&1 | head -20

echo ""
echo "===== 等 10 秒, 看地址是否被 udhcpc 抹掉 ====="
sleep 10
echo "spitun0:"
ip addr show spitun0 | grep -E "inet |UP"
echo "回程路由:"
ip route | grep -E "spitun|192.168.3.64" || echo "  (无回程路由)"
echo "udhcpc 进程:"
ps | grep "[u]dhcpc" || echo "  无 udhcpc"

echo ""
echo "===== 守卫是否装上 ====="
head -8 /usr/share/udhcpc/default.script

echo ""
echo "===== 再等 20 秒, 二次确认地址还在 ====="
sleep 20
ip addr show spitun0 | grep -E "inet " && echo "  ✅ 地址稳定" || echo "  ❌ 地址又没了"
echo "udhcpc:"
ps | grep "[u]dhcpc" || echo "  无"

echo ""
echo "===== 看门狗 ====="
/etc/init.d/S24spinet_wd restart 2>&1 | head -5
sleep 3
/etc/init.d/S24spinet_wd status 2>&1

#!/bin/sh
echo "===== 1) 隧道接口 (关键: 地址必须在) ====="
ip addr show spitun0 | grep -E "inet |UP"

echo ""
echo "===== 2) 回程路由 ====="
ip route

echo ""
echo "===== 3) udhcpc 守卫 ====="
grep -c 'spitun0|tun0' /usr/share/udhcpc/default.script
echo "udhcpc 进程: $(ps | grep -c '[u]dhcpc')"

echo ""
echo "===== 4) 看门狗实例数 (应该只有 1 个真正的循环) ====="
ps | grep '[S]24spinet_wd' | grep -v 'sh -c'

echo ""
echo "===== 5) C5 状态 ====="
cat /sys/class/net/spitun0/c3_status

echo ""
echo "===== 6) 内核诊断 (bad_magic 是否还在涨) ====="
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]* bad_csum=[0-9]*' | tail -2

echo ""
echo "===== 7) 服务进程 ====="
ps | grep -E '[w]eb_server|[m]ediamtx|[r]kipc'

echo ""
echo "===== 8) 摄像头 RTSP ====="
python3 - <<'PY'
import socket
try:
    s = socket.create_connection(("127.0.0.1", 554), timeout=6)
    s.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n")
    d = s.recv(4096).decode(errors="replace")
    print("  ", d.split("\r\n")[0])
    import re
    m = re.search(r"a=fmtp:\d+ .*", d)
    print("   SDP 有视频轨:", "m=video" in d)
    s.close()
except Exception as e:
    print("   RTSP 失败:", e)
PY

echo ""
echo "===== 9) mediamtx 是否拿到流 ====="
tail -4 /tmp/mediamtx_local.log 2>/dev/null

echo ""
echo "===== 10) 网页 ====="
python3 -c "
import urllib.request, json
try:
    d = json.load(urllib.request.urlopen('http://127.0.0.1/api/status', timeout=8))
    print('   online =', d.get('online'), ' 电池 =', d['tel']['bat_v'], 'V')
    print('   C5在线 =', d['tel']['c3']['online'], ' rssi =', d['tel']['c3']['rssi'])
    print('   GPS =', d['tel']['gps']['state_text'])
except Exception as e:
    print('   失败:', e)
"
echo ""
echo "===== 11) 磁盘 ====="
df -h /userdata | tail -1

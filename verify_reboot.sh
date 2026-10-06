#!/bin/sh
echo "=========== 重启后自动恢复验证 ==========="
echo "uptime: $(cat /proc/uptime | awk '{print $1}') 秒"
echo ""
echo "--- 1) lo 回环 (决定视频能否出流) ---"
ip addr show lo | grep -E 'inet ' | sed 's/^/  /'
echo ""
echo "--- 2) spitun0 隧道地址 ---"
ip addr show spitun0 2>/dev/null | grep -E 'inet |UP' | sed 's/^/  /'
echo ""
echo "--- 3) udhcpc 守卫 ---"
if grep -q 'spitun0|tun0' /usr/share/udhcpc/default.script 2>/dev/null; then
  echo "  已装 (udhcpc 清不掉隧道地址)"
else
  echo "  !! 未装"
fi
echo ""
echo "--- 4) 回程路由 ---"
ip route | grep -E 'spitun|192.168.3.64' | sed 's/^/  /'
echo ""
echo "--- 5) C5 状态 ---"
cat /sys/class/net/spitun0/c3_status 2>/dev/null | sed 's/^/  /'
echo ""
echo "--- 6) 服务进程 ---"
ps | grep -E '[w]eb_server|[m]ediamtx|[r]kipc|[S]24spinet_wd' | sed 's/^/  /'
echo ""
echo "--- 7) 监听端口 ---"
netstat -tln | grep -E ':80 |:554 |:8554|:8889|:8189|:8888' | sed 's/^/  /'
echo ""
echo "--- 8) rkipc RTSP (经 lo) ---"
python3 - <<'PY'
import socket
try:
    s = socket.create_connection(("127.0.0.1", 554), timeout=6)
    s.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n")
    d = s.recv(4096).decode(errors="replace")
    print("   ", d.split("\r\n")[0], "| m=video:", "m=video" in d)
    s.close()
except Exception as e:
    print("    失败:", e)
PY
echo ""
echo "--- 9) mediamtx 是否已接入视频源 ---"
grep -E 'ready:|ERR' /tmp/mediamtx_local.log 2>/dev/null | tail -5 | sed 's/^/  /'
echo ""
echo "--- 10) 隧道 bad_magic ---"
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]* bad_csum=[0-9]*' | tail -1 | sed 's/^/  /'
echo ""
echo "--- 11) 本机自连 ---"
python3 - <<'PY'
import socket
for ip, port in [('127.0.0.1',80), ('127.0.0.1',554), ('127.0.0.1',8889), ('127.0.0.1',8888)]:
    try:
        s = socket.create_connection((ip, port), timeout=4); print("    %s:%d OK" % (ip,port)); s.close()
    except Exception as e:
        print("    %s:%d 失败 %s" % (ip,port,e))
PY
echo ""
echo "--- 12) 磁盘 ---"
df -h /userdata | tail -1 | sed 's/^/  /'

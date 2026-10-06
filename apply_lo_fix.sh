#!/bin/sh
chmod 755 /etc/init.d/S20lo
echo "===== 应用 lo 修复 ====="
/etc/init.d/S20lo start
echo "status: $(/etc/init.d/S20lo status)"

echo ""
echo "===== lo 接口 ====="
ip addr show lo | grep -E 'inet|UP'

echo ""
echo "===== 自连复测 ====="
python3 - <<'PY'
import socket
for ip, port in [('127.0.0.1',554), ('127.0.0.1',80), ('127.0.0.1',8889)]:
    try:
        s = socket.create_connection((ip, port), timeout=4)
        print("  %s:%d  OK" % (ip, port))
        s.close()
    except Exception as e:
        print("  %s:%d  失败 %s" % (ip, port, e))
PY

echo ""
echo "===== RTSP 真的能出流吗 ====="
python3 - <<'PY'
import socket
try:
    s = socket.create_connection(("127.0.0.1", 554), timeout=6)
    s.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n")
    d = s.recv(4096).decode(errors="replace")
    print("   ", d.split("\r\n")[0])
    print("    SDP 有 m=video:", "m=video" in d)
    s.close()
except Exception as e:
    print("    失败:", e)
PY

echo ""
echo "===== 重启 mediamtx, 让它重新连 RTSP ====="
/etc/init.d/S25mediamtx restart 2>&1 | head -10

echo ""
echo "===== mediamtx 日志 ====="
tail -15 /tmp/mediamtx_local.log 2>/dev/null

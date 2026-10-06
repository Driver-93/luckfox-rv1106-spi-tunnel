#!/bin/sh
# 确认图传真的出画面 (不能只看 WHEP 返回 204 —— 那只说明 API 在)。
# 用 RTSP DESCRIBE 拿 SDP, 有 SDP 才说明真的有流。
echo "=========== 1) RTSP 是否真的有流 ==========="
python3 - <<'PY'
import socket
try:
    s=socket.create_connection(("127.0.0.1",554),timeout=6)
    s.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n")
    s.settimeout(6)
    d=s.recv(4096).decode(errors='replace')
    if 'm=video' in d or '200 OK' in d:
        print("  ✓ RTSP 有流")
        for line in d.split("\r\n")[:14]:
            if line.strip(): print("    "+line)
    else:
        print("  ✗ RTSP 无流, 响应:", d[:200])
    s.close()
except Exception as e:
    print("  ✗ RTSP 连接失败:", e)
PY

echo
echo "=========== 2) rkipc 有没有 bind 失败 ==========="
P=$(ps | grep '[r]kipc' | head -1 | awk '{print $1}')
echo "  rkipc pid=$P"
ls -l /tmp/rkipc.log 2>/dev/null && echo "  --- /tmp/rkipc.log 错误行 ---" && grep -iE 'bind|address already|rtsp_new|error|fail' /tmp/rkipc.log 2>/dev/null | head -10

echo
echo "=========== 3) 编码器输出字节数 (真的在编码?) ==========="
echo "  554 队列: $(netstat -ltn 2>/dev/null | grep ':554' | awk '{print "Recv-Q="$2" Send-Q="$3}')"
echo "  rkipc CPU: $(top -b -n1 | grep '[r]kipc' | awk '{print $7}')"

echo
echo "=========== 4) mediamtx 拉流状态 ==========="
ls -l /root/mediamtx/ 2>/dev/null | head -5
echo "  --- 从 mediamtx 侧看有没有连上 rkipc ---"
for f in /tmp/mediamtx.log /root/mediamtx/mediamtx.log /userdata/mediamtx.log; do
  [ -f "$f" ] && { echo "  $f:"; tail -15 "$f" | sed 's/^/    /'; }
done

echo
echo "=========== 5) 8899/8889 由谁监听 ==========="
netstat -ltnp 2>/dev/null | grep -E ':8889|:8888|:8189' | sed 's/^/  /'

echo
echo "=========== 6) WHEP 请求实测 (拿 SDP answer) ==========="
python3 - <<'PY'
import urllib.request, json
try:
    body=json.dumps({"type":"offer","sdp":"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"}).encode()
    req=urllib.request.Request("http://127.0.0.1:8889/car/whep", data=body,
        headers={"Content-Type":"application/json"})
    r=urllib.request.urlopen(req, timeout=8)
    print("  HTTP", r.status)
    print("  SDP 前 200:", r.read()[:200].decode(errors='replace'))
except Exception as e:
    print("  (构造假 offer 被拒属正常):", type(e).__name__, str(e)[:120])
PY
echo "done"

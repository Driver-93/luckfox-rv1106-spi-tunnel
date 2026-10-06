#!/bin/sh
OUT=/tmp/mtx2.log
: > $OUT
exec >> $OUT 2>&1

echo "=== $(date) ==="
echo "--- 清理 ---"
for p in $(ps | grep '[m]ediamtx' | awk '{print $1}'); do kill -9 "$p" 2>/dev/null; echo "  killed $p"; done
sleep 2

echo "--- lo 确认 ---"
ip addr show lo | grep 'inet '

echo "--- 554 确认 ---"
netstat -tln | grep ':554 '

echo "--- 启动 mediamtx ---"
cd /root/mediamtx || exit 1
setsid ./mediamtx /root/mediamtx/mediamtx.yml </dev/null >/tmp/mediamtx_local.log 2>&1 &
echo "  pid=$!"

sleep 20

echo "--- 进程 ---"
ps | grep '[m]ediamtx' || echo "  (没有)"

echo "--- 日志 ---"
cat /tmp/mediamtx_local.log 2>/dev/null

echo "--- 端口 ---"
netstat -tln | grep -E ':8889|:8189|:8888|:8554'

echo "--- 自连 8889 ---"
python3 - <<'PY'
import socket
for ip, port in [('127.0.0.1',8889), ('127.0.0.1',8189)]:
    try:
        s = socket.create_connection((ip, port), timeout=4); print("  %s:%d OK" % (ip,port)); s.close()
    except Exception as e:
        print("  %s:%d 失败 %s" % (ip, port, e))
PY

echo "=== 完成 $(date) ==="

#!/bin/sh
# 纯 shell 版 rkipc 安全重启 (无 python, 避免 SIGKILL 拖累)
LOG=/tmp/npu_restart3.log
exec >"$LOG" 2>&1

kill554() {
    # 杀掉持有 554 LISTEN socket 的进程 (按 inode 反查)
    inode=$(awk '$2 ~ /:022A$/ && $4 == "0A" {print $10}' /proc/net/tcp 2>/dev/null | head -1)
    [ -z "$inode" ] && return 0
    for p in $(ls /proc | grep -E '^[0-9]+$'); do
        ls -l /proc/$p/fd 2>/dev/null | grep -q "socket:\[$inode\]" && {
            echo "killing holder $p $(cat /proc/$p/comm 2>/dev/null)"
            kill -9 $p 2>/dev/null
        }
    done
}

echo "=== step1: kill rkipc/mediamtx ==="
killall -9 rkipc 2>/dev/null
killall -9 mediamtx 2>/dev/null
sleep 2

echo "=== step2: wait 554 free (max 40s) ==="
i=0
while [ $i -lt 40 ]; do
    awk '$2 ~ /:022A$/ && $4 == "0A" {found=1} END{exit !found}' /proc/net/tcp 2>/dev/null || { echo "554 free after ${i}s"; break; }
    [ $((i % 5)) -eq 4 ] && kill554
    i=$((i+1)); sleep 1
done

echo "=== step3: start rkipc ==="
cd /userdata 2>/dev/null || cd /
LD_LIBRARY_PATH=/oem/usr/lib:/oem/lib setsid /oem/usr/bin/rkipc -a /oem/usr/share/iqfiles >/tmp/rkipc.log 2>&1 &
sleep 12
echo "rkipc pid: $(pidof rkipc)"
grep -cE 'bind socket to address failed' /tmp/rkipc.log
grep -E 'ROCKIVA_BA_Init|model data not found' /tmp/rkipc.log | head -3

echo "=== step4: verify RTSP responds ==="
python3 - <<'PYEOF'
import socket
try:
    c = socket.create_connection(('127.0.0.1', 554), timeout=10)
    c.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    c.settimeout(10)
    print("RTSP:", c.recv(64).decode('utf-8', 'ignore').splitlines()[0])
    c.close()
except Exception as e:
    print("RTSP FAIL:", e)
PYEOF

echo "=== step5: start mediamtx ==="
cd /root/mediamtx 2>/dev/null && setsid ./mediamtx mediamtx.yml >/tmp/mediamtx_local.log 2>&1 &
sleep 5
echo "mediamtx pid: $(pidof mediamtx)"
cat /proc/rknpu/load 2>/dev/null
echo DONE

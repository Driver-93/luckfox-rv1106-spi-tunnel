#!/bin/sh
# 最终验收: 确认 NPU 人/狗检测完整可用, 且不影响控制延迟。
LOG=/mnt/sdcard/npu/accept.log
exec > "$LOG" 2>&1
echo "=========== $(date '+%H:%M:%S') 最终验收 ==========="

P=$(ps | grep '[r]kipc' | grep -v grep | head -1 | awk '{print $1}')
echo "rkipc pid=$P"

echo
echo "=== 1) NPU 是否被 rkipc 持有 ==="
echo "  rknpu Used = $(lsmod | grep rknpu | awk '{print $3}')"
for f in /proc/$P/fd/*; do
  t=$(readlink $f 2>/dev/null)
  case "$t" in *rknpu*) echo "  fd -> $t" ;; esac
done 2>/dev/null

echo
echo "=== 2) 检测相关线程 (iva_main_loop / RkipcNpuOsd / RkipcGetIVS) ==="
for t in /proc/$P/task/*; do
  n=$(cat $t/comm 2>/dev/null)
  case "$n" in
    *iva*|*Npu*|*NPU*|*IVS*|*rockiva*) echo "  [检测] $n" ;;
  esac
done

echo
echo "=== 3) rockiva 初始化结果 (唯一的判据) ==="
grep -n 'ROCKIVA' /tmp/rkipc.log 2>/dev/null | head -8

echo
echo "=== 4) 有没有缺模型错误 ==="
n=$(grep -c 'object_detection_pfp model data not found' /tmp/rkipc.log 2>/dev/null)
echo "  'model data not found' 次数 = $n  (>0 就是失败)"

echo
echo "=== 5) 配置 ==="
grep -nE '^enable_npu|^npu_fps' /userdata/rkipc.ini | sed 's/^/  /'
grep -n 'rockiva_model_type' /userdata/rkipc.ini | sed 's/^/  /'

echo
echo "=== 6) 图传 ==="
python3 - <<'PY'
import socket
try:
    s=socket.create_connection(("127.0.0.1",554),timeout=6)
    s.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n")
    s.settimeout(6); d=s.recv(4096).decode(errors='replace')
    print("  RTSP:", "有流 ✓" if 'm=video' in d else "无流 ✗")
except Exception as e:
    print("  RTSP 失败:", e)
PY

echo
echo "=== 7) 资源占用 ==="
echo "  内存:"; free | sed -n 2p | sed 's/^/    /'
echo "  rkipc:"; grep -E 'VmRSS' /proc/$P/status 2>/dev/null | sed 's/^/    /'
echo "  top:"; top -b -n1 | grep -E '^CPU' | sed 's/^/    /'
echo "  uptime: $(cut -d. -f1 /proc/uptime)s"
echo "  554: $(netstat -ltn 2>/dev/null | grep -c ':554')"

echo
echo "=== 8) 控制延迟 ==="
python3 /mnt/sdcard/npu/lat.py FINAL 2>&1 | sed 's/^/  /'

echo "=========== 结束 ==========="

#!/bin/sh
echo "=== 紧急停车 ==="
python3 - <<'PY'
import urllib.request, json
def post(path, body):
    req = urllib.request.Request("http://127.0.0.1" + path,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=8).read().decode()[:200]
    except Exception as e:
        return "失败: %s" % e
print("  cmd stop  ->", post("/api/cmd",  {"c": "stop",  "s": 0}))
print("  cmd brake ->", post("/api/cmd",  {"c": "brake", "s": 0}))
print("  cmd stop2 ->", post("/api/cmd",  {"c": "stop",  "s": 0}))
PY

echo ""
echo "=== 当前状态 ==="
python3 -c "
import urllib.request, json
try:
    d = json.load(urllib.request.urlopen('http://127.0.0.1/api/status', timeout=8))
    print('  mode   =', d.get('mode'))
    print('  dir    =', d.get('dir'))
    print('  speed  =', d.get('speed'))
    print('  vx/vy/w=', d.get('vx'), d.get('vy'), d.get('w'))
    print('  电池   =', d['tel']['bat_v'], 'V')
except Exception as e:
    print('  失败:', e)
"

echo ""
echo "=== 电机相关进程/线程 ==="
ps | grep -E '[w]eb_server|[r]kipc' | sed 's/^/  /'

echo ""
echo "=== web 日志尾部 ==="
tail -25 /tmp/web.log 2>/dev/null

echo ""
echo "=== 板子负载 ==="
uptime
echo "--- D 状态任务 ---"
ps -o pid,stat,comm 2>/dev/null | awk '$2 ~ /D/' | head -20

echo ""
echo "=== 隧道状态 ==="
cat /sys/class/net/spitun0/c3_status
echo ""
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]* bad_csum=[0-9]*' | tail -1

echo ""
echo "=== 看门狗 ==="
/etc/init.d/S21wdt status

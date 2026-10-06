#!/bin/sh
# 控制延迟分解测量
#
# 用户感觉"控制延迟高", 但之前测的是 /api/status 的往返 (55-157ms)。
# 控制延迟是另一回事, 由几段叠加:
#   1. 网页轮询间隔 (setInterval) —— 我刚从 100ms 改成 200ms
#   2. 请求在途串行化 (flushControl 同一时刻只允许一个请求)
#   3. 网络往返 (经 SPI 隧道 + C5 NAPT)
#   4. 板子处理
# 这里把 2/3/4 量出来, 1 是前端常量可以直接算。

echo "=== 1) /api/move 往返 (连续 30 次, 串行) ==="
python3 - <<'PY'
import urllib.request, json, time

B = "http://127.0.0.1"
body = json.dumps({"vx": 0.0, "vy": 0.0, "w": 0.0, "s": 0}).encode()
hdr = {"Content-Type": "application/json"}

ts = []
for i in range(30):
    t0 = time.time()
    req = urllib.request.Request(B + "/api/move", data=body, headers=hdr)
    urllib.request.urlopen(req, timeout=10).read()
    ts.append((time.time() - t0) * 1000)

ts.sort()
n = len(ts)
print("  最小 %.0fms  中位 %.0fms  P90 %.0fms  最大 %.0fms"
      % (ts[0], ts[n//2], ts[int(n*0.9)], ts[-1]))
print("  平均 %.0fms" % (sum(ts)/n))
PY

echo ""
echo "=== 2) 丢包/重传检查 (同一连接连续发, 看有没有卡顿尖峰) ==="
python3 - <<'PY'
import http.client, json, time
c = http.client.HTTPConnection("127.0.0.1", 80, timeout=10)
body = json.dumps({"vx": 0.0, "vy": 0.0, "w": 0.0, "s": 0})
ts = []
for i in range(40):
    t0 = time.time()
    c.request("POST", "/api/move", body, {"Content-Type": "application/json"})
    c.getresponse().read()
    ts.append((time.time() - t0) * 1000)
c.close()
ts.sort()
n = len(ts)
print("  keep-alive 单连接: 最小 %.0fms 中位 %.0fms P90 %.0fms 最大 %.0fms"
      % (ts[0], ts[n//2], ts[int(n*0.9)], ts[-1]))
over100 = sum(1 for x in ts if x > 100)
print("  >100ms 的次数: %d / %d" % (over100, n))
PY

echo ""
echo "=== 3) 板子侧处理耗时 (端到端减去网络) ==="
python3 - <<'PY'
import urllib.request, json, time
B = "http://127.0.0.1"
# /api/ping 几乎不做任何事, 用它当"纯网络+框架"基线
ts = []
for i in range(20):
    t0 = time.time()
    urllib.request.urlopen(B + "/api/ping", timeout=10).read()
    ts.append((time.time() - t0) * 1000)
ts.sort()
print("  /api/ping  中位 %.0fms  (纯框架开销)" % ts[len(ts)//2])
PY

echo ""
echo "=== 4) 电机驱动实际耗时 (板子内部) ==="
python3 - <<'PY'
import sys, time
sys.path.insert(0, "/userdata/car")
try:
    from car_motor import FourMotor
    m = FourMotor()
    t0 = time.time()
    for i in range(200):
        m.drive(0.0, 0.0, 0.0, 0)
    dt = (time.time() - t0) / 200 * 1000
    print("  motor.drive() 平均耗时: %.2f ms" % dt)
    t0 = time.time()
    for i in range(200):
        m.stop()
    dt = (time.time() - t0) / 200 * 1000
    print("  motor.stop()  平均耗时: %.2f ms" % dt)
    m.stop(); m.close()
except Exception as e:
    print("  失败:", e)
PY

echo ""
echo "=== 5) 当前 C5 链路质量 ==="
cat /sys/class/net/spitun0/c3_status
echo ""
dmesg | grep -o 'frames=[0-9]* ok=[0-9]* fail=[0-9]* bad_magic=[0-9]*' | tail -1

echo ""
echo "=== 6) 板子负载 ==="
uptime

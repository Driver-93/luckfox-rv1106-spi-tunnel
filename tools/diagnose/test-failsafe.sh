#!/bin/sh
echo "=== 失控保护测试 ==="
echo "预期: 发一次移动指令后不再发, 1 秒内板子自动停车, failsafe +1"
echo ""

python3 - <<'PY'
import urllib.request, json, time

B = "http://127.0.0.1"

def post(path, body):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=8).read().decode())

def get(path):
    return json.loads(urllib.request.urlopen(B + path, timeout=8).read().decode())

def show(tag):
    d = get("/api/status")
    print("  %-22s dir=%-6s vx=%s vy=%s w=%s  cmd_age=%ss  failsafe=%s"
          % (tag, d["dir"], d["vx"], d["vy"], d["w"],
             d.get("cmd_age"), d.get("failsafe")))

show("初始")

print("")
print("--- 1) 发一次移动指令 (vx=0.5, s=0) ---")
# s=0 保证轮子不转, 但 STATE 里 vx 非零 -> 仍算"在动", 能测出保护逻辑
r = post("/api/move", {"vx": 0.5, "vy": 0.0, "w": 0.0, "s": 0})
print("  返回:", r)
show("刚发完")

print("")
print("--- 2) 0.5 秒后 (还在超时窗口内, 应该还在动) ---")
time.sleep(0.5)
show("等0.5s")

print("")
print("--- 3) 再等 1.5 秒 (超过 1 秒超时, 应该被自动停车) ---")
time.sleep(1.5)
show("等2.0s")

print("")
print("--- 4) 连续发 5 次心跳 (每次间隔 0.3s, 应该保持不动) ---")
for i in range(5):
    post("/api/move", {"vx": 0.5, "vy": 0.0, "w": 0.0, "s": 0})
    time.sleep(0.3)
show("心跳中")

print("")
print("--- 5) 停止心跳 1.5 秒 ---")
time.sleep(1.5)
show("心跳停止后")

print("")
print("--- 6) 明确停车 ---")
print("  ", post("/api/cmd", {"c": "stop"}))
time.sleep(0.5)
show("stop后")
PY

echo ""
echo "=== 失控保护日志 ==="
tail -10 /userdata/failsafe.log 2>/dev/null || echo "  (暂无日志 = 还没触发过)"

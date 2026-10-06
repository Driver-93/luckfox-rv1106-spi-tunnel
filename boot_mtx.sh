#!/bin/sh
# 完全脱离会话地启动 mediamtx, 全程记录到 /tmp/mtx_boot.log
OUT=/tmp/mtx_boot.log
: > $OUT
exec >> $OUT 2>&1

echo "=== $(date) 开始 ==="

BIN=/root/mediamtx/mediamtx
CONF=/root/mediamtx/mediamtx.yml

echo "BIN 存在: $([ -f $BIN ] && echo yes || echo no)  可执行: $([ -x $BIN ] && echo yes || echo no)"
echo "CONF 存在: $([ -f $CONF ] && echo yes || echo no)"
ls -l $BIN $CONF 2>&1

echo "--- 554 是否在听 ---"
netstat -tln | grep ':554 ' || echo "  554 不在听"

echo "--- 清理旧进程 ---"
for p in $(ps | grep '[m]ediamtx' | awk '{print $1}'); do
  kill -9 "$p" 2>/dev/null
  echo "  killed $p"
done

echo "--- 启动 ---"
cd /root/mediamtx || exit 1
setsid ./mediamtx "$CONF" </dev/null >/tmp/mediamtx_local.log 2>&1 &
echo "  已发出启动命令, pid=$!"

echo "--- 等 15 秒 ---"
sleep 15

echo "--- 进程 ---"
ps | grep '[m]ediamtx' || echo "  (没有 mediamtx 进程)"

echo "--- mediamtx 日志 ---"
tail -30 /tmp/mediamtx_local.log 2>/dev/null || echo "  (无日志文件)"

echo "--- 监听端口 ---"
netstat -tln | grep -E ':8889|:8189|:8888|:8554' || echo "  (没有相关端口)"

echo "=== $(date) 结束 ==="

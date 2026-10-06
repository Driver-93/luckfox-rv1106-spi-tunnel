#!/bin/sh
# 推流看门狗 (修正版)
#
# 原版 bug: `while kill -0 $FP && [ $W -lt 60 ]` 中 W 计到 60*5=300 秒,
#   无论 ffmpeg 健康与否, 到点一律 kill -9 重启
#   -> 每 5 分钟必然断流一次。日志实证: reconnect 间隔精确 5分02秒,
#      与推流状态完全无关。
#
# 修正: 只在 ffmpeg "真退出" 或 "真僵死" 时重启, 去掉无条件定时重启。
#   僵死判据: /proc/PID/stat 的 utime+stime 长时间不增长 (进程卡死不动)
#   注: 本板 /proc/PID/io 不可读, 故用 CPU 时间作判据。
#
# 用法: 由 /etc/init.d/S97relay 调用

LOG=/tmp/relay.log
STALL_INT=10
STALL_LIMIT=6          # 连续 6*10=60 秒 CPU 无增长 -> 判定僵死

log() { echo "$(date) $*" >>"$LOG"; }

start_ffmpeg() {
  /root/ffmpeg -hide_banner -loglevel error -fflags nobuffer -flags low_delay \
    -analyzeduration 0 -probesize 2000 -rtsp_transport tcp \
    -i rtsp://127.0.0.1:554/live/0 -an -c:v copy \
    -f rtsp -rtsp_transport tcp -timeout 5000000 \
    rtsp://YOUR_SERVER_IP:8554/car >>"$LOG" 2>&1 &
  echo $!
}

# 输出 "utime+stime" (CPU jiffies); 进程不存在时输出空
cpu_jiffies() {
  st=$(cat /proc/$1/stat 2>/dev/null) || { echo ""; return; }
  echo "$st" | awk '{print $14 + $15}'
}

log "watchdog start: stall=${STALL_INT}s x ${STALL_LIMIT} (no periodic restart)"
while :; do
  FP=$(start_ffmpeg)
  log "ffmpeg started pid=$FP"
  last=$(cpu_jiffies $FP)
  stall=0
  while kill -0 $FP 2>/dev/null; do
    sleep $STALL_INT
    cur=$(cpu_jiffies $FP)
    [ -z "$cur" ] && break          # 进程已消失
    if [ "$cur" = "$last" ]; then
      stall=$((stall+1))
      if [ $stall -ge $STALL_LIMIT ]; then
        log "ffmpeg pid=$FP STALLED ${STALL_INT}x${STALL_LIMIT}s, restarting"
        kill -9 $FP 2>/dev/null
        break
      fi
    else
      stall=0
      last="$cur"
    fi
  done
  kill -9 $FP 2>/dev/null
  wait $FP 2>/dev/null
  log "ffmpeg pid=$FP exited, reconnect in 2s"
  sleep 2
done

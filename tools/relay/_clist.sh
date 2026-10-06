#!/bin/sh
echo "=== 进程总数 ==="
ls /proc | grep -c '^[0-9]'
echo "=== ffmpeg 及 supervisor 相关进程 ==="
ps -o pid,args | grep -E 'ffmpeg|supervisor|S99carffmpeg|carpush' | grep -v grep
echo "=== top 前8 ==="
top -bn1 2>/dev/null | head -12

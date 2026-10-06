#!/bin/bash
# 云端转码: mediamtx car(HEVC) -> H264 Baseline(最高兼容) -> carH264
LOG=/tmp/cloudtrans.log
while true; do
  echo "[$(date)] start transcode" >>"$LOG" 2>&1
  ffmpeg -hide_banner -loglevel error -fflags nobuffer -flags low_delay \
    -rtsp_transport tcp -i rtsp://127.0.0.1:8554/car \
    -an -c:v libx264 -preset veryfast -tune zerolatency -profile:v baseline -level 3.0 \
    -pix_fmt yuv420p -g 12 -keyint_min 12 -sc_threshold 0 -b:v 1500k \
    -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/carH264 >>"$LOG" 2>&1
  echo "[$(date)] ffmpeg exit, retry 3s" >>"$LOG" 2>&1
  sleep 3
done

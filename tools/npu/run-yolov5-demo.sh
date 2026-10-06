#!/bin/sh
cd /mnt/sdcard/npu || exit 1
export LD_LIBRARY_PATH=/oem/usr/lib:/mnt/sdcard/npu/lib
echo "launch: pwd=$(pwd)" > /tmp/launch.log
echo "launch: ld=$LD_LIBRARY_PATH" >> /tmp/launch.log
echo "launch: model=$(ls -l model/yolov5.rknn 2>&1)" >> /tmp/launch.log
./luckfox_pico_yolov5 model/yolov5.rknn >> /tmp/launch.log 2>&1 &
echo "launch: child pid=$!" >> /tmp/launch.log
sleep 300

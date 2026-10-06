#!/bin/sh
# 关键问题: NPU 本身到底能不能跑推理?
# 之前 enable_npu=1 会重启, 但 rkipc 并没打开 NPU —— 所以那个开关可能是
# 坏路径, 而不是 NPU 不能用。
#
# 这里用最干净的方式验证: 写一个最小的 C 程序, 只做 RKNN 初始化 +
# 查询 SDK 版本, 不碰摄像头、不碰 rkipc。如果这个能跑, 说明 NPU 可用。
echo "=========== 1) 先看 NPU 驱动状态 ==========="
lsmod | grep rknpu
ls -l /dev/rknpu
echo "  --- NPU 相关 sysfs/debugfs ---"
find /sys -iname '*npu*' 2>/dev/null | head -10
ls /sys/kernel/debug/ 2>/dev/null | head -20
echo "  --- dmesg 里的 rknpu ---"
dmesg | grep -iE 'rknpu|npu' | head -10
echo "  (空=驱动加载时没打印)"

echo
echo "=========== 2) librknnmrt.so 导出的 API ==========="
strings /oem/usr/lib/librknnmrt.so 2>/dev/null | grep -E '^rknn_(init|query|destroy|duplicate|inputs_set|outputs_set|run|outputs_release)' | sort -u | head -20

echo
echo "=========== 3) 版本字符串 ==========="
strings /oem/usr/lib/librknnmrt.so 2>/dev/null | grep -iE 'version|1\.[0-9]\.[0-9]|2\.[0-9]\.[0-9]' | head -10

echo
echo "=========== 4) 有没有编译器 (能不能现场编个小程序测) ==========="
which gcc cc tcc 2>/dev/null || echo "  无编译器 (板子上没有)"

echo
echo "=========== 5) demo 二进制能不能只做 --version 之类的 ==========="
cd /mnt/sdcard/npu 2>/dev/null && ls -l

echo
echo "=========== 6) 用 python ctypes 直接调 rknn_init (最干净) ==========="
python3 - <<'PY'
import ctypes, ctypes.util, os
# 板子是 uClibc, find_library 可能不好使, 直接给路径
cands = ["/oem/usr/lib/librknnmrt.so", "librknnmrt.so"]
lib = None
for c in cands:
    try:
        lib = ctypes.CDLL(c)
        print("  已加载:", c)
        break
    except Exception as e:
        print("  加载失败", c, e)
if lib is None:
    raise SystemExit("  无法加载 librknnmrt.so")

# rknn_query(ctx, cmd, buf, size)
try:
    fn = lib.rknn_query
    print("  rknn_query 符号存在")
except AttributeError as e:
    print("  找不到 rknn_query:", e)

# 尝试查询 SDK 版本 (RKNN_QUERY_SDK_VERSION = 1)
class Ver(ctypes.Structure):
    _fields_ = [("api_version", ctypes.c_char*256), ("drv_version", ctypes.c_char*256)]

buf = Ver()
ret = lib.rknn_query(ctypes.c_void_p(0), ctypes.c_uint(1), ctypes.byref(buf), ctypes.c_uint(ctypes.sizeof(buf)))
print("  rknn_query(SDK_VERSION) ret=%d (负数=需要有效 ctx, 属正常)" % ret)
if buf.api_version:
    print("  api_version:", buf.api_version.decode(errors='replace'))
if buf.drv_version:
    print("  drv_version:", buf.drv_version.decode(errors='replace'))
PY

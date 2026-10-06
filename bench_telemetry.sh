#!/bin/sh
# 逐个体测 telemetry 里的每个采集函数, 找出到底谁慢。
python3 - <<'PY'
import sys, time
sys.path.insert(0, "/userdata/car")
import web_server as W

def bench(name, fn, n=30):
    try:
        fn()  # 预热
    except Exception as e:
        print("  %-16s 预热失败: %s" % (name, e))
        return
    t0 = time.time()
    for _ in range(n):
        try:
            fn()
        except Exception:
            pass
    dt = (time.time() - t0) / n * 1000
    print("  %-16s %8.2f ms/次   (5Hz 下占 %.1f%% CPU)"
          % (name, dt, dt * 5 / 10))

print("=== telemetry 各函数耗时 ===")
bench("read_adc_mv",  lambda: W.read_adc_mv(W.ADC_CH))
bench("check_4g",     W.check_4g)
bench("net_info",     W.net_info)
bench("c3_status",    W.c3_status)
bench("build_status", W.build_status)
bench("_failsafe",    W._failsafe_check)

print("")
print("=== 对比: 1Hz 与 5Hz 的总开销 ===")
PY

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据 GPS 位置自动设置时区。

需求: 板子会随车移动, 时区不该写死 CST-8。GPS 定位成功后, 用经纬度算出
所在时区, 写进 /etc/TZ 并把 TZ 导出给相关进程。

为什么不装 tzdata 用 Asia/Shanghai:
    板子的根文件系统里没有 zoneinfo 数据库 (/usr/share/zoneinfo 不存在),
    装不下也没必要。POSIX 的 TZ 写法 (如 CST-8) 只要一个固定偏移, 对
    中国全境完全够用 —— 中国只有一个时区 (UTC+8), 没有夏令时。

核心做法:
    1. 从 GPS 拿到经纬度
    2. 经度 / 15 得到粗略时区偏移 (每 15 度一个时区)
    3. 用「时区边界表」修正 —— 纯按经度算在中国会得到 UTC+7 左右
       (中国横跨约 73-135 度), 而实际统一用 UTC+8。所以对已知国家
       做覆盖。
    4. 写 /etc/TZ, 并通过 signal 让上层知道变了

注意 POSIX TZ 的符号是**反的**:
    UTC+8 时区 (北京) 写作 CST-8
    UTC-5 时区 (纽约) 写作 EST5
"""

import glob
import io
import os
import re
import subprocess
import time

GPS_LAT = None
GPS_LON = None

TZFILE = "/etc/TZ"


def read_gps():
    """从 web_server 的状态里拿经纬度 (它已经在后台解析 NMEA 了)."""
    try:
        import json
        import urllib.request
        d = json.load(urllib.request.urlopen("http://127.0.0.1/api/status",
                                             timeout=4))
        g = d.get("tel", {}).get("gps", {})
        if g.get("fix") and g.get("lat") is not None:
            return float(g["lat"]), float(g["lon"])
    except Exception:
        pass
    return None, None


def tz_from_lonlat(lat, lon):
    """(lat, lon) -> (POSIX TZ 字符串, 描述)。"""
    # 1) 纯经度估算: 每个时区 15 度, 东经为正
    raw = lon / 15.0
    offset = int(round(raw))

    # 2) 国家/地区覆盖 —— 这些地方的法定时区与经度推算差别很大
    #    中国全境统一 UTC+8 (经度跨 73~135, 按经度会算出 UTC+5..+9)
    name = None

    if 73.0 <= lon <= 135.0 and 18.0 <= lat <= 54.0:
        offset, name = 8, "中国"
    elif 122.0 <= lon <= 154.0 and 20.0 <= lat <= 46.0:
        offset, name = 9, "日本/韩国"
    elif 100.0 <= lon <= 142.0 and -11.0 <= lat <= 20.0:
        offset, name = 8, "东南亚西部"        # 泰国/越南/印尼西部
    elif 114.0 <= lon <= 130.0 and -11.0 <= lat <= 20.0:
        offset, name = 8, "东南亚东部"        # 菲律宾/马来西亚
    elif 68.0 <= lon <= 90.0 and 6.0 <= lat <= 37.0:
        offset, name = 5.5, "印度/斯里兰卡"
    elif -170.0 <= lon <= -50.0 and 15.0 <= lat <= 72.0:
        # 北美: 粗略按经度, 再修正常见区域
        if -125.0 <= lon <= -114.0:
            offset, name = -8, "美西"
        elif -114.0 <= lon <= -100.0:
            offset, name = -7, "美山地"
        elif -100.0 <= lon <= -85.0:
            offset, name = -6, "美中部"
        elif -85.0 <= lon <= -65.0:
            offset, name = -5, "美东部"
        else:
            name = "北美"
    elif -10.0 <= lon <= 40.0 and 35.0 <= lat <= 72.0:
        offset, name = 1, "欧洲中部"
    elif 112.0 <= lon <= 154.0 and -45.0 <= lat <= -10.0:
        if 138.0 <= lon <= 154.0:
            offset, name = 10, "澳东"
        else:
            offset, name = 8, "澳西"
    elif 165.0 <= lon <= 180.0 and -48.0 <= lat <= -33.0:
        offset, name = 12, "新西兰"

    # 3) 生成 POSIX TZ 串。
    #    POSIX 里 "CST-8" 表示 UTC+8 —— 符号与直觉相反。
    if offset == 0:
        tz = "UTC0"
    elif float(offset).is_integer():
        n = int(offset)
        # 用 UTC 偏移写法, 避免依赖时区缩写
        tz = "UTC%s%d" % ("-" if n > 0 else "+", abs(n))
    else:
        # 半小时时区 (印度 +5.5) -> UTC-5:30
        whole = int(abs(offset))
        tz = "UTC%s%d:%02d" % ("-" if offset > 0 else "+", whole, 30)

    desc = name or ("经度推算 %.0f°" % lon)
    return tz, "%s (UTC%+g)" % (desc, offset)


def current_tz():
    try:
        return io.open(TZFILE).read().strip()
    except Exception:
        return None


def apply_tz(tz):
    if current_tz() == tz:
        return False
    io.open(TZFILE, "w").write(tz + "\n")
    os.environ["TZ"] = tz
    # busybox date 读 /etc/TZ; 已运行的进程不会自动更新, 但 rkipc 的 OSD
    # 是每秒重新取系统时间的, 所以改 /etc/TZ 后它下次刷新就会用新时区。
    return True


def main():
    print("GPS 自动时区: 启动")
    last = None
    while True:
        try:
            lat, lon = read_gps()
            if lat is None:
                time.sleep(20)
                continue
            tz, desc = tz_from_lonlat(lat, lon)
            if tz != last:
                changed = apply_tz(tz)
                print("GPS 自动时区: %.4f,%.4f -> %s %s%s"
                      % (lat, lon, tz, desc,
                         " (已写入 /etc/TZ)" if changed else " (无变化)"))
                last = tz
                # 输出到日志便于排查
                try:
                    with io.open("/userdata/tz_auto.log", "a") as f:
                        f.write("%s %.4f,%.4f %s %s\n"
                                % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   lat, lon, tz, desc))
                except Exception:
                    pass
        except Exception as e:
            print("GPS 自动时区错误:", e)
        time.sleep(30)


if __name__ == "__main__":
    main()

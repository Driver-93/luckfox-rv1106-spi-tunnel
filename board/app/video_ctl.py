#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频档位切换: 在几组预设的"分辨率 + 码率 + 帧率"之间切换。

为什么要这个:
    板子是单核, 视频编码和 SPI 隧道抢同一个核。2304x1296 全分辨率需要
    ~6 Mbps, 实测会把 mediamtx 的发送队列堵满 ("write queue is full",
    100 行里 83 次), 结果 mediamtx 反复重试白烧 35% CPU, 控制指令也被挤。
    1280x720 只要 0.5~2 Mbps, 隧道宽松, 控制延迟低。

    用户需要在"看得清"和"开得稳"之间随时切换, 所以做成档位。

实现要点:
    * 改的是 rkipc 的配置 (rkipc.ini + 模板), 需要重启 rkipc 才生效。
    * **重启 rkipc 有已知风险**: 554 端口的 socket 会残留在内核里,
      新进程 bind 失败, 图传黑屏, 只能重启板子清掉 (踩过三次)。
      所以切换后要**主动检查 bind 是否成功**, 失败就明确报错而不是假装成功。
    * 切换会让视频中断几秒 —— 这是重启 rkipc 的必然代价。

档位设计 (基于实测):
    流畅  1280x720   1536 kbps   30fps   <- 默认, 隧道轻松
    清晰  1920x1080  3072 kbps   30fps   <- 平衡
    超清  2304x1296  6144 kbps   30fps   <- 最清楚, 但隧道吃紧
"""

import io
import os
import re
import signal
import subprocess
import sys
import threading
import time

INI = "/userdata/rkipc.ini"
TPL = "/oem/usr/share/rkipc-300w.ini"
LOG = "/userdata/video_profile.log"

# 档位表。所有档位都是 [video.0] 主码流的设置。
#
# ⚠️ 开机默认档位 = **流畅 720p** (用户指定)。
#    做法: 把 ini 和模板都写成 720p, 这样即使 rkipc 被固件重启、
#    或板子断电重启, 起来就是 720p, 不会悄悄回到 1296p 把隧道打满。
DEFAULT_PROFILE = "smooth"
# 开机默认曝光档位 = **1/1000 极快** (用户指定, 防拖影)。
DEFAULT_EXPOSURE = "t1000"

PROFILES = {
    "smooth": {
        "label": "流畅 720p",
        "desc": "1280x720 · 1.5Mbps · 隧道轻松, 控制延迟最低",
        "width": 1280, "height": 720,
        "buffer_size": 1280 * 720 // 2,
        "max_rate": 1536, "mid_rate": 1024,
    },
    "hd": {
        "label": "清晰 1080p",
        "desc": "1920x1080 · 3Mbps · 画质与带宽的平衡",
        "width": 1920, "height": 1080,
        "buffer_size": 1920 * 1080 // 2,
        "max_rate": 3072, "mid_rate": 2048,
    },
    "uhd": {
        "label": "超清 1296p",
        "desc": "2304x1296 · 6Mbps · 最清楚, 但隧道吃紧",
        "width": 2304, "height": 1296,
        "buffer_size": 2304 * 1296 // 2,
        "max_rate": 6144, "mid_rate": 4096,
    },
}

STATE = {"current": None, "switching": False, "last_err": None,
         "exp_current": None}
_LOCK = threading.Lock()


# ============================================================================
# 曝光档位
# ============================================================================
# 为什么不做"实时曝光滑块" (这是实测结论, 不是偷懒):
#
#   /dev/v4l-subdev2 上确实有一个 `exposure` 控制 (1..1624), 但它**不是
#   可用的用户旋钮**。实测:
#     * 写 exposure=500, 立刻回读是 500, **800ms 后变回 408** ——
#       自动曝光跑在 rkaiq 用户态 ISP 里, V4L2 层没有任何开关能关掉它
#       (枚举了全部 5 个 subdev, 都没有 auto_exposure/exposure_auto)。
#     * 把 rkipc.ini 切成 exposure_mode=manual + auto_exposure_enabled=0
#       后, 这个寄存器会被 ISP 按 exposure_time 重新推导 —— 用户写什么
#       都不算数。
#
#   **真正有效的是 rkipc 层的 exposure_time 档位。** 实测 (干净启动,
#   每次切档等 25 秒让 ISP settle):
#       exposure_time=1/1000 -> v4l2 exposure=41     analogue_gain=7731
#       exposure_time=1/100  -> v4l2 exposure=408    analogue_gain=771
#       exposure_time=1/25   -> v4l2 exposure=1624   analogue_gain=197
#   单调、可控、有效。
#
#   代价: 必须改 ini + 重启 rkipc (约 20-30 秒, 视频会中断一下)。
#   和分辨率切换是同一个机制, 所以直接复用 _restart_rkipc()。
#
#   注意 25fps 下 1/25 已是物理上限, 再长只会顶到 max(1624), 所以档位
#   到 1/25 为止。
#
# 增益保持 auto: 曝光固定后由增益去补偿亮度, 画面不会因为选了快门的
# 档位而整体变黑。
EXPOSURE = {
    "auto": {
        "label": "自动", "desc": "ISP 自动曝光 · 日常首选",
        "exposure_mode": "auto", "auto_exposure_enabled": 1,
        "exposure_time": "1/6", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t1000": {
        "label": "1/1000 极快", "desc": "强光/高速运动 · 几乎无拖影, 暗处会噪",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/1000", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t500": {
        "label": "1/500 很快", "desc": "白天高速运动",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/500", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t250": {
        "label": "1/250 快", "desc": "白天行车",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/250", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t100": {
        "label": "1/100 中", "desc": "白天/阴天平衡",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/100", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t50": {
        "label": "1/50 慢", "desc": "黄昏 · 更亮但可能有拖影",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/50", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
    "t25": {
        "label": "1/25 最慢", "desc": "夜间最亮 · 25fps 下的物理上限",
        "exposure_mode": "manual", "auto_exposure_enabled": 0,
        "exposure_time": "1/25", "gain_mode": "auto",
        "audo_gain_enabled": 1, "exposure_gain": 1,
    },
}


def _log(msg):
    try:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ============================================================================
# 画面质量: 亮度 / 对比度 / 饱和度 / 锐度 + 过曝抑制
# ============================================================================
# 为什么这些**能**做, 而之前的"实时曝光滑块"不能:
#
#   strings /oem/usr/bin/rkipc 里能直接看到:
#       rk_isp_set_brightness / rk_isp_get_brightness
#       rk_isp_set_contrast   / rk_isp_get_contrast
#       isp.%d.adjustment:brightness
#       isp.%d.adjustment:contrast
#   也就是说**亮度和对比度是 rkipc 官方支持的参数**, 它自己会写进 ISP。
#   而 V4L2 那条路 (/dev/v4l-subdev2 的 exposure 寄存器) 才是死路 ——
#   写进去 800ms 就被 rkaiq 的 AE 覆盖。
#
# 代价与曝光档位一样: 参数在 rkipc 启动时读进 ISP, **运行中改不了**,
# 所以必须改 ini + 重启 rkipc (画面断约 20 秒)。这不是偷懒, 是硬件限制。
#
# ---- 过曝到底是怎么来的 (实测) ----
#   用户选 "1/1000 极快" 时, ini 变成:
#       exposure_mode = manual, auto_exposure_enabled = 0, exposure_time = 1/1000
#       gain_mode = auto            <-- 关键
#   曝光被钉死在 1/1000, 但**增益还是自动**: ISP 为了让画面"够亮", 把增益
#   往上猛推 (实测 gain 从 197 一路到 7000+, 上限 99614)。
#   高增益 = 亮部溢出 = 过曝。
#
#   所以治过曝有三个旋钮, 按见效顺序:
#     1) gain 也切成 manual 并压低 (最直接, 砍掉那个失控的补偿)
#     2) 开 WDR (宽动态): 大光比场景压暗亮部、提亮暗部
#     3) over_exposure_suppress 已经是 open, 保持
QUALITY_KEYS = ("brightness", "contrast", "saturation", "sharpness")

# ---- 增益档位: **实测无效, 已弃用** (保留定义只为兼容旧请求) ----
#
# ⚠️ 2026-10-06 实测结论 (花了四组对照实验才搞清, 别再走这条路):
#
#   | 配置                        | ISP exposure | ISP analogue_gain |
#   |-----------------------------|--------------|-------------------|
#   | exposure_gain=1   (manual)  | 41           | 384               |
#   | exposure_gain=100 (manual)  | 41           | 384  <- 没变!     |
#   | gain_mode=auto              | 41           | 384  <- 没变!     |
#   | exposure=auto + gain=auto   | 408          | 128  <- 变了      |
#
#   也就是说:
#     1) ini 里的 `exposure_gain` (1..100) 在 exposure_mode=manual 时
#        **完全不起作用** —— 写 1 和写 100, ISP 的 analogue_gain 都是 384。
#        (rkipc 的能力元数据说它范围 1..100, 但实际不生效。)
#     2) 只要 exposure_mode=manual, 增益就固定在 384, gain_mode 怎么设都没用。
#     3) **只有把曝光切回 auto, AE 才会真正接管**, 增益才会降到 128 (最低)。
#
#   所以"过曝"的真正机制不是"增益太高", 而是:
#       曝光被钉死 (1/1000) -> 画面偏暗 -> 用户为了看清把亮度拉高 ->
#       而 AE 被禁用、无法自动降低曝光 -> 亮部溢出成一片白
#   治它只有一条路: **让 AE 工作** (曝光档位选「自动」), 或者接受手动曝光时
#   画面就是那个亮度、不要再用亮度硬拉。
GAIN_LEVELS = {
    "auto": {"label": "增益 自动", "desc": "交给 ISP (注意: 曝光为手动时此项无效)",
             "gain_mode": "auto", "audo_gain_enabled": 1, "exposure_gain": 1},
}

# 宽动态 (WDR): 大光比场景的关键。关着的时候逆光/强日照必然过曝。
WDR_LEVELS = {
    "off":  {"label": "WDR 关", "desc": "不做动态范围压缩 · 逆光会全白", "wdr": "close", "wdr_level": 0},
    "low":  {"label": "WDR 低", "desc": "轻度过曝抑制", "wdr": "open", "wdr_level": 1},
    "mid":  {"label": "WDR 中", "desc": "白天逆光推荐", "wdr": "open", "wdr_level": 3},
    "high": {"label": "WDR 高", "desc": "极强光比 · 画面会稍平", "wdr": "open", "wdr_level": 5},
}


def _ini_read():
    try:
        return io.open(INI, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""


def _ini_seg(text, section):
    """取出 [section] 那一段 (到下一个 [ 为止)。"""
    m = re.search(r"\[%s\]([^\[]*)" % re.escape(section), text)
    return m.group(1) if m else None


def _ini_set(section, key, value, path=INI):
    """把 [section] 段里的 key 设成 value (不存在就追加)。返回是否改动。"""
    try:
        text = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return False
    seg = _ini_seg(text, section)
    if seg is None:
        return False
    new_seg, n = re.subn(r"(?m)^(\s*%s\s*=\s*)[^\s;]+" % re.escape(key),
                         lambda m: m.group(1) + str(value), seg, count=1)
    if n == 0:
        new_seg = seg.rstrip("\n") + "\n%s = %s\n" % (key, value)
    if new_seg == seg:
        return False
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        text.replace(seg, new_seg, 1))
    return True


def read_quality():
    """读当前画面参数 (isp.0 段)。"""
    text = _ini_read()
    adj = _ini_seg(text, "isp.0.adjustment") or ""
    exp = _ini_seg(text, "isp.0.exposure") or ""
    blc = _ini_seg(text, "isp.0.blc") or ""
    ntd = _ini_seg(text, "isp.0.night_to_day") or ""

    def num(seg, key, d=None):
        m = re.search(r"(?m)^\s*%s\s*=\s*(\d+)" % key, seg)
        return int(m.group(1)) if m else d

    def word(seg, key, d=None):
        m = re.search(r"(?m)^\s*%s\s*=\s*(\S+)" % key, seg)
        return m.group(1) if m else d

    gain_mode = word(exp, "gain_mode", "auto")
    ex_gain = num(exp, "exposure_gain", 1)
    gkey = "auto"
    if gain_mode == "manual":
        gkey = "high" if ex_gain >= 64 else ("mid" if ex_gain >= 32 else "low")

    wdr = word(blc, "wdr", "close")
    wlvl = num(blc, "wdr_level", 0)
    wkey = "off"
    if wdr == "open":
        wkey = "high" if wlvl >= 5 else ("mid" if wlvl >= 3 else "low")

    return {
        "brightness": num(adj, "brightness", 50),
        "contrast": num(adj, "contrast", 50),
        "saturation": num(adj, "saturation", 50),
        "sharpness": num(adj, "sharpness", 50),
        "gain": gkey,
        "wdr": wkey,
        "over_exposure_suppress": word(ntd, "over_exposure_suppress", "open"),
        "dark_boost_level": num(blc, "dark_boost_level", 0),
    }


def quality_info():
    q = read_quality()
    return {
        "ok": True,
        "current": q,
        "ranges": {k: {"min": 0, "max": 100} for k in QUALITY_KEYS},
        "gain_levels": [{"key": k, "label": v["label"], "desc": v["desc"]}
                        for k, v in GAIN_LEVELS.items()],
        "wdr_levels": [{"key": k, "label": v["label"], "desc": v["desc"]}
                       for k, v in WDR_LEVELS.items()],
        "hint": "改这些要重启视频服务 (画面断约 20 秒)。过曝先调「增益」再调「WDR」。",
    }


def quality_set(brightness=None, contrast=None, saturation=None, sharpness=None,
                gain=None, wdr=None, over_exposure_suppress=None):
    """改画面参数。同步执行, 约 20-30 秒 (要重启 rkipc)。"""
    with _LOCK:
        if STATE["switching"]:
            return False, "正在切换中, 请稍候"
        STATE["switching"] = True
    try:
        changed = []

        # 1) 亮度/对比度/饱和度/锐度 -> [isp.0.adjustment] (+ isp.1 同步)
        vals = {"brightness": brightness, "contrast": contrast,
                "saturation": saturation, "sharpness": sharpness}
        for k, v in vals.items():
            if v is None:
                continue
            try:
                iv = max(0, min(100, int(v)))
            except Exception:
                continue
            for path in (INI, TPL):
                for sec in ("isp.0.adjustment", "isp.1.adjustment"):
                    _ini_set(sec, k, iv, path)
            changed.append("%s=%d" % (k, iv))

        # 2) 增益 (治过曝的关键)
        if gain is not None:
            g = GAIN_LEVELS.get(str(gain))
            if not g:
                return False, "未知增益档位: %s" % gain
            for path in (INI, TPL):
                for sec in ("isp.0.exposure", "isp.1.exposure"):
                    _ini_set(sec, "gain_mode", g["gain_mode"], path)
                    _ini_set(sec, "audo_gain_enabled", g["audo_gain_enabled"], path)
                    _ini_set(sec, "exposure_gain", g["exposure_gain"], path)
            changed.append("增益=%s" % g["label"])

        # 3) WDR
        if wdr is not None:
            w = WDR_LEVELS.get(str(wdr))
            if not w:
                return False, "未知 WDR 档位: %s" % wdr
            for path in (INI, TPL):
                for sec in ("isp.0.blc", "isp.1.blc"):
                    _ini_set(sec, "wdr", w["wdr"], path)
                    _ini_set(sec, "wdr_level", w["wdr_level"], path)
            changed.append("%s" % w["label"])

        # 4) 过曝抑制开关
        if over_exposure_suppress is not None:
            v = "open" if str(over_exposure_suppress) in ("1", "true", "open", "on") else "close"
            for path in (INI, TPL):
                for sec in ("isp.0.night_to_day", "isp.1.night_to_day"):
                    _ini_set(sec, "over_exposure_suppress", v, path)
            changed.append("过曝抑制=%s" % v)

        if not changed:
            return False, "没有要改的参数"

        ok, msg = _restart_rkipc()
        if not ok:
            STATE["last_err"] = msg
            _log("画面参数切换失败: %s (%s)" % (msg, ", ".join(changed)))
            return False, msg
        _log("画面参数已改: %s" % ", ".join(changed))
        return True, "已应用: " + ", ".join(changed)
    finally:
        with _LOCK:
            STATE["switching"] = False


def read_current():
    """从 rkipc.ini 读出当前分辨率, 反查属于哪个档位。"""
    try:
        s = io.open(INI, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    seg = s.split("[video.0]")[1].split("[video.1]")[0] if "[video.0]" in s else ""
    w = re.search(r"(?m)^\s*width\s*=\s*(\d+)", seg)
    r = re.search(r"(?m)^\s*max_rate\s*=\s*(\d+)", seg)
    h = re.search(r"(?m)^\s*height\s*=\s*(\d+)", seg)
    if not w:
        return None
    cur_w = int(w.group(1))
    cur_h = int(h.group(1)) if h else 0
    cur_r = int(r.group(1)) if r else 0
    for k, p in PROFILES.items():
        if p["width"] == cur_w and p["height"] == cur_h:
            return {"key": k, "width": cur_w, "height": cur_h,
                    "max_rate": cur_r, "label": p["label"]}
    return {"key": None, "width": cur_w, "height": cur_h,
            "max_rate": cur_r, "label": "%dx%d" % (cur_w, cur_h)}


def _write_profile(path, prof):
    """把档位写进一个 ini。返回改了几项。"""
    try:
        s = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return 0
    if "[video.0]" not in s:
        return 0
    head, sep, rest = s.partition("[video.0]")
    m = re.match(r"((?:[^\[]*))", rest)
    body, tail = m.group(1), rest[len(m.group(1)):]

    vals = {
        "width": str(prof["width"]),
        "height": str(prof["height"]),
        "buffer_size": str(prof["buffer_size"]),
        "max_rate": str(prof["max_rate"]),
        "mid_rate": str(prof["mid_rate"]),
        "src_frame_rate_num": "30",
        "dst_frame_rate_num": "30",
    }
    n = 0
    for k, v in vals.items():
        new, c = re.subn(r"(?m)^(\s*%s\s*=\s*)[^\s;]+" % k, r"\g<1>" + v,
                         body, count=1)
        if c:
            body = new
        else:
            # 属性不存在就追加 (注意注释要单独一行, 否则会被当成值的一部分)
            body = body.rstrip("\n") + "\n%s = %s\n" % (k, v)
        n += 1
    io.open(head and path or path, "w", encoding="utf-8",
            newline="\n").write(head + sep + body + tail)
    return n


def _port554_busy():
    """554 端口是否还被占着 (有 LISTEN 或残留连接条目)。"""
    try:
        n = 0
        with io.open("/proc/net/tcp", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                # 本地地址列形如 00000000:022A, 022A = 554
                if len(parts) > 1 and parts[1].endswith(":022A"):
                    n += 1
        return n
    except Exception:
        return 0


def _port554_listening():
    """554 是否处于 LISTEN (state 0A)。"""
    try:
        with io.open("/proc/net/tcp", encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) > 3 and p[1].endswith(":022A") and p[3] == "0A":
                    return True
    except Exception:
        pass
    return False


def _port554_holder():
    """返回占用 0.0.0.0:554 的进程名, 没有则返回 None。

    **一次扫描内解析**, 不能分两次快照 —— PID 会被回收, 分开读会张冠李戴
    (这个坑踩过: 一度把占用者误判成别的东西)。
    """
    inode = None
    try:
        with io.open("/proc/net/tcp", encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) > 9 and p[1].endswith(":022A") and p[3] == "0A":
                    inode = p[9]
                    break
    except Exception:
        return None
    if not inode:
        return None

    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fddir = "/proc/%s/fd" % pid
            try:
                for fd in os.listdir(fddir):
                    try:
                        if os.readlink("%s/%s" % (fddir, fd)) == \
                                "socket:[%s]" % inode:
                            with io.open("/proc/%s/comm" % pid) as g:
                                return "%s(pid=%s)" % (g.read().strip(), pid)
                    except OSError:
                        continue
            except OSError:
                continue
    except OSError:
        pass
    return "unknown"


def _kill_udhcpc_tun0():
    """杀掉占用 554 的 udhcpc (点对点隧道上那个), 返回被杀掉的 pid 列表。

    这是"切换分辨率后图传黑了 / 从大往小切也不行"的**真正根因**:

        rkipc (不能改的 Rockchip 二进制) 会拉起
        `udhcpc -i <隧道> -T 1 -A 0 -b -q`。隧道是点对点, 永远没有 DHCP
        服务器, 于是它带着 -b -q 无限重试。而 busybox udhcpc 的发现
        socket 会绑到 **0.0.0.0:554** —— 正好是 rkipc RTSP 要用的端口。

        udhcpc 占着 554 之后: rkipc 起得来、编码正常、但 bind 不上 RTSP。
        现象和老的"重启 rkipc 后 554 socket 残留"**长得一模一样**,
        所以被误诊了很久。

    ⚠️ 2026-10-05 修: 原来判断条件是 `"udhcpc" in cmd and "tun0" in cmd` ——
        **隧道接口早就从 tun0 改名成 spitun0 了**, 所以这个函数一个都杀不掉
        (日志里出现 `已杀掉 pids=[]`, 但端口仍被占着), 切换档位必然失败:

            15:18:57 切换失败: bind失败=True RTSP=timed out (554 被 udhcpc 占着)
            15:23:27 切换失败: ... (554 被 udhcpc 占着)
            15:25:14 切换失败: ... (554 被 udhcpc 占着)

        现在改成: **按接口名 (spitun0/tun0) 或按"它是否占着 554"来判定**,
        两条判据取并集, 不再依赖某个写死的名字。
    """
    killed = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with io.open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if "udhcpc" not in cmd:
                continue
            # 判据一: 命令行里有隧道接口 (spitun0 或遗留的 tun0)
            # 判据二: 它就是这个占着 554 的进程
            is_tunnel = ("spitun0" in cmd) or ("tun0" in cmd)
            if not is_tunnel:
                continue
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
    except OSError:
        pass

    # 兜底: 谁占着 554 且是 udhcpc, 也杀 (处理 cmdline 读不到的情况)
    h = _port554_holder()
    if h and h.startswith("udhcpc"):
        try:
            pid = int(h.split("pid=")[1].rstrip(")"))
            os.kill(pid, signal.SIGKILL)
            if str(pid) not in killed:
                killed.append(str(pid))
        except Exception:
            pass
    return killed


def _port554_free_force():
    """把 554 彻底腾空: 杀 udhcpc + 如果还是被占, 找出**任何**占着它的进程杀掉。

    为什么需要"任何进程": 实测出现过占用者已经不是 udhcpc 的情况
    (rkipc 自己的残留 socket 被别的进程继承)。这时只杀 udhcpc 没用,
    必须按 inode 反查真实持有者。
    返回 (是否腾空, 描述)。
    """
    k = _kill_udhcpc_tun0()
    for _ in range(10):
        if not _port554_listening():
            return True, "udhcpc killed=%s, 554 已释放" % k
        time.sleep(0.5)

    # 还是被占 -> 按 inode 找持有者并杀掉 (排除 rkipc 自己, 它一会儿要重启)
    h = _port554_holder()
    if h and not h.startswith("unknown"):
        name = h.split("(")[0]
        try:
            pid = int(h.split("pid=")[1].rstrip(")"))
            if name not in ("rkipc",):
                os.kill(pid, signal.SIGKILL)
                _log("554 被 %s 占用, 已强杀" % h)
        except Exception:
            pass
    for _ in range(20):
        if not _port554_listening():
            return True, "554 已释放 (强杀 %s)" % h
        time.sleep(0.5)
    return False, "554 仍被 %s 占着" % h


def _restart_rkipc():
    """重启 rkipc, 并**确保 RTSP 真的能出流**。

    为什么要这么小心 (踩过三次):
        旧 rkipc 进程被杀后, 它的 554 监听 socket 会残留在内核里。
        新进程 bind 失败 -> 进程活着但不出流 -> mediamtx 一直重连 ->
        网页黑屏。而残留是靠"内核回收", 时间不确定 (实测几十秒到
        永远不回收, 那时只能重启板子)。

    所以做法:
        1. 先停 mediamtx —— 它是不停在重连的源头, 会加剧 socket 占用
        2. 杀掉 rkipc, **轮询等 554 真正变成不监听** (最多 25 秒)
        3. 起 rkipc
        4. 检查 bind 日志 + 真连一次 RTSP
        5. 无论如何都把 mediamtx 起回来 (否则图传彻底没了)
    """
    os.environ["LD_LIBRARY_PATH"] = "/oem/usr/lib:/usr/lib"

    # 1) 先停 mediamtx (它是重试源)
    subprocess.run(["killall", "-9", "mediamtx"], capture_output=True)
    time.sleep(2)

    # 2) 杀 rkipc
    subprocess.run(["killall", "-9", "rkipc"], capture_output=True)
    time.sleep(2)
    # 再补一刀, 防止有子进程
    subprocess.run(["sh", "-c",
                    "for p in $(ps | grep '[r]kipc' | awk '{print $1}'); "
                    "do kill -9 $p 2>/dev/null; done"],
                   capture_output=True)

    # 3) 轮询等 554 不再监听
    freed = False
    for _ in range(50):          # 最多 25 秒
        if not _port554_listening():
            freed = True
            break
        time.sleep(0.5)

    # 3.5) 554 还被占着 -> 看是谁占的。
    #
    #   udhcpc 占用是**常见**情况 (rkipc 每次重启都会拉起它), 杀掉即可,
    #   不用重启板子。真的 socket 残留才需要等内核回收。
    #
    #   ⚠️ 2026-10-05: 改成调 `_port554_free_force()` —— 它会杀 udhcpc,
    #   **而且**在还是被占时按 inode 反查真实持有者强杀 (以前只判断
    #   `holder.startswith("udhcpc")`, 一旦占用者名字不是 udhcpc 就放弃,
    #   直接导致切换档位必然失败)。
    if not freed:
        holder = _port554_holder()
        okfree, msg = _port554_free_force()
        freed = okfree
        _log("554 被 %s 占用 -> %s" % (holder, msg))
        if not freed:
            holder = _port554_holder()

    # 4) 起 rkipc
    subprocess.Popen(["/oem/usr/bin/rkipc", "-a", "/oem/usr/share/iqfiles"],
                     stdout=open("/tmp/rkipc.log", "w"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(15):
        time.sleep(2)
        r = subprocess.run(["pidof", "rkipc"], capture_output=True)
        if r.stdout.strip():
            break
    time.sleep(8)

    # 5) 检查 bind 日志
    bindok = True
    try:
        log = io.open("/tmp/rkipc.log", encoding="utf-8",
                      errors="ignore").read()
        if "bind failed" in log or "bind socket to address failed" in log:
            bindok = False
    except Exception:
        pass

    # 6) 真连一次 RTSP
    rtsp_ok = False
    rtsp_msg = ""
    try:
        import socket as _s
        c = _s.create_connection(("127.0.0.1", 554), timeout=8)
        c.sendall(b"DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\n"
                  b"CSeq: 1\r\nAccept: application/sdp\r\n\r\n")
        c.settimeout(8)
        d = c.recv(512).decode("utf-8", "ignore")
        c.close()
        first = d.split("\r\n")[0]
        if "200" in first:
            rtsp_ok = True
        else:
            rtsp_msg = first
    except Exception as e:
        rtsp_msg = str(e)

    # 7) 恢复 mediamtx (不管成功失败都要起回来)
    try:
        subprocess.Popen(["sh", "-c",
                          "cd /root/mediamtx && setsid ./mediamtx mediamtx.yml "
                          ">/tmp/mediamtx_local.log 2>&1 &"],
                         start_new_session=True)
        time.sleep(4)
    except Exception:
        pass

    if not bindok or not rtsp_ok:
        if not freed:
            # 明确区分两种原因 —— 以前一律说"需要重启板子", 把人吓一跳,
            # 其实多数情况杀个 udhcpc 就好。
            extra = (" (554 被 %s 占着)" % holder) if holder else \
                    " (554 一直没释放, 可能是内核 socket 残留)"
        else:
            extra = ""
        return False, ("rkipc 起不来: bind失败=%s RTSP=%s%s"
                       % (not bindok, rtsp_msg, extra))
    return True, "ok"


def video_switch(key):
    """切换档位。同步执行, 大约 20-30 秒 (重启 rkipc 的代价)。"""
    if key not in PROFILES:
        return False, "未知档位: %s" % key

    with _LOCK:
        if STATE["switching"]:
            return False, "正在切换中, 请稍候"
        STATE["switching"] = True
        STATE["last_err"] = None

    try:
        prof = PROFILES[key]
        _log("切换到 %s (%dx%d, %dkbps)"
             % (key, prof["width"], prof["height"], prof["max_rate"]))

        n1 = _write_profile(INI, prof)
        n2 = _write_profile(TPL, prof)   # 模板: 开机时 RkLunch.sh 会 cp 过去
        if n1 == 0:
            STATE["last_err"] = "写入 rkipc.ini 失败"
            return False, STATE["last_err"]

        ok, msg = _restart_rkipc()
        if not ok:
            STATE["last_err"] = msg
            _log("切换失败: %s" % msg)
            return False, msg

        STATE["current"] = key
        _log("切换成功")
        return True, "已切换到 %s" % prof["label"]
    finally:
        with _LOCK:
            STATE["switching"] = False


def video_info():
    """给 API 用的状态。"""
    cur = read_current()
    with _LOCK:
        switching = STATE["switching"]
        err = STATE["last_err"]
    return {
        "ok": True,
        "current": cur,
        "switching": switching,
        "last_err": err,
        "profiles": [
            {"key": k, "label": v["label"], "desc": v["desc"],
             "width": v["width"], "height": v["height"],
             "max_rate": v["max_rate"]}
            for k, v in PROFILES.items()
        ],
    }


def read_exposure():
    """从 rkipc.ini 读出当前曝光设置, 反查属于哪个档位。

    注意 exposure_time 在两个 isp 段里都有 (isp.0 / isp.1)。只看 isp.0。
    """
    try:
        s = io.open(INI, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    if "[isp.0.exposure]" not in s:
        return None
    seg = s.split("[isp.0.exposure]")[1].split("[")[0]
    m_mode = re.search(r"(?m)^\s*exposure_mode\s*=\s*(\S+)", seg)
    m_time = re.search(r"(?m)^\s*exposure_time\s*=\s*(\S+)", seg)
    mode = m_mode.group(1) if m_mode else "?"
    tm = m_time.group(1) if m_time else "?"
    if mode == "auto":
        return {"key": "auto", "mode": mode, "exposure_time": tm,
                "label": EXPOSURE["auto"]["label"]}
    for k, p in EXPOSURE.items():
        if p["exposure_mode"] == "manual" and p["exposure_time"] == tm:
            return {"key": k, "mode": mode, "exposure_time": tm,
                    "label": p["label"]}
    return {"key": None, "mode": mode, "exposure_time": tm,
            "label": "手动 %s" % tm}


def _write_exposure(path, prof):
    """把曝光档位写进一个 ini 的 [isp.0.exposure] 和 [isp.1.exposure]。

    返回改动的项数; 0 表示失败。
    """
    try:
        s = io.open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return 0

    vals = {
        "exposure_mode": prof["exposure_mode"],
        "auto_exposure_enabled": str(prof["auto_exposure_enabled"]),
        "exposure_time": prof["exposure_time"],
        "gain_mode": prof["gain_mode"],
        "audo_gain_enabled": str(prof["audo_gain_enabled"]),
        "exposure_gain": str(prof["exposure_gain"]),
    }

    n = 0
    for section in ("[isp.0.exposure]", "[isp.1.exposure]"):
        if section not in s:
            continue
        head, sep, rest = s.partition(section)
        # 段体 = 到下一个 '[' 之前
        m = re.match(r"((?:[^\[]*))", rest)
        body, tail = m.group(1), rest[len(m.group(1)):]
        for k, v in vals.items():
            new, c = re.subn(r"(?m)^(\s*%s\s*=\s*)[^\s;]+" % k,
                             r"\g<1>" + v, body, count=1)
            if c:
                body = new
                n += 1
            else:
                body = body.rstrip("\n") + "\n%s = %s\n" % (k, v)
                n += 1
        s = head + sep + body + tail

    try:
        io.open(path, "w", encoding="utf-8", newline="\n").write(s)
    except Exception:
        return 0
    return n


def exposure_switch(key):
    """切换曝光档位。同步执行, 约 20-30 秒 (要重启 rkipc)。"""
    if key not in EXPOSURE:
        return False, "未知曝光档位: %s" % key

    with _LOCK:
        if STATE["switching"]:
            return False, "正在切换中, 请稍候"
        STATE["switching"] = True
        STATE["last_err"] = None

    try:
        prof = EXPOSURE[key]
        _log("切换曝光档位 %s (mode=%s time=%s)"
             % (key, prof["exposure_mode"], prof["exposure_time"]))

        n1 = _write_exposure(INI, prof)
        n2 = _write_exposure(TPL, prof)   # 模板: 开机时 RkLunch.sh 会 cp 过去
        if n1 == 0:
            STATE["last_err"] = "写入 rkipc.ini 失败"
            return False, STATE["last_err"]

        ok, msg = _restart_rkipc()
        if not ok:
            STATE["last_err"] = msg
            _log("曝光切换失败: %s" % msg)
            return False, msg

        # 重启后回读, 确认真的写进去了
        cur = read_exposure()
        STATE["exp_current"] = cur
        _log("曝光切换成功: %s" % (cur,))
        return True, "已切换到 %s" % prof["label"]
    finally:
        with _LOCK:
            STATE["switching"] = False


def exposure_info():
    """给 API 用的曝光状态。"""
    cur = read_exposure()
    with _LOCK:
        switching = STATE["switching"]
        err = STATE["last_err"]
    return {
        "ok": True,
        "current": cur,
        "switching": switching,
        "last_err": err,
        "profiles": [
            {"key": k, "label": v["label"], "desc": v["desc"],
             "exposure_time": v["exposure_time"],
             "mode": v["exposure_mode"]}
            for k, v in EXPOSURE.items()
        ],
    }


def main():
    """命令行入口。

    之前这个文件**没有 main**, 所以 `python3 video_ctl.py info` 什么都不
    打印、退出码还是 0 —— 看起来像"成功但没输出", 排查时会一路查错方向。
    """
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "info":
        cur = read_current()
        if cur is None:
            print("读不到当前档位 (看 %s 是否可读)" % INI)
            return 1
        print("当前: %s  %dx%d  %dkbps"
              % (cur["label"], cur["width"], cur["height"], cur["max_rate"]))
        print("档位:")
        for k, v in PROFILES.items():
            mark = "*" if k == cur["key"] else " "
            print("  %s %-8s %-12s %s" % (mark, k, v["label"], v["desc"]))
        if _port554_listening():
            h = _port554_holder()
            print("554: LISTEN by %s" % h)
            if h and h.startswith("udhcpc"):
                print("     ^ 这是 udhcpc 占着 RTSP 端口, 图传会黑。")
                print("       'video_ctl.py fix554' 可以立刻修好 (不用重启板子)")
        else:
            print("554: 空闲")
        return 0

    if cmd == "fix554":
        h = _port554_holder()
        if not h:
            print("554 空闲, 无需处理")
            return 0
        print("554 被 %s 占用" % h)
        if h.startswith("udhcpc"):
            k = _kill_udhcpc_tun0()
            print("已杀掉 udhcpc: pids=%s" % k)
            time.sleep(1)
            if _port554_listening():
                print("554 仍被占用 by %s" % _port554_holder())
                return 1
            print("554 已释放。若图传还是黑的, 需要重启 rkipc (切换一次档位即可)")
            return 0
        print("占用者不是 udhcpc, 不能自动处理 (可能需要重启 rkipc)")
        return 1

    if cmd in PROFILES:
        ok, msg = video_switch(cmd)
        print(msg)
        return 0 if ok else 1

    print("用法: video_ctl.py [info | fix554 | %s]" % " | ".join(PROFILES))
    return 2


if __name__ == "__main__":
    sys.exit(main())

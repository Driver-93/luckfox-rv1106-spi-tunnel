#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板子系统状态采集 (给网页的"系统状态"面板用)。

设计约束 (很重要, 别违反):
  1. **必须廉价**。这个函数会在 telemetry_loop 里每秒跑一次。
     历史上把重采集提到 5Hz 直接把单核打满, 控制延迟暴涨 (见
     telemetry_loop 的注释)。所以这里只读 /proc 和 /sys 的小文件,
     绝不 fork 子进程、绝不遍历大目录。
  2. **CPU 占用要自己算差分**。/proc/stat 是累计值, 必须保存上一次采样
     再求差。跨度太短会因为 jiffy 粒度产生噪声, 所以内部保存历史。
  3. **NPU 状态**。rknpu 模块的 Used 计数在 /sys/module/rknpu/refcnt
     (有些内核在 holders 目录), 都试一下; 拿不到就如实返回 None,
     不要编数字。
  4. **任何一项失败都不能让整个 status 挂掉** —— 每项独立 try。

用法:
    import sys_stats
    st = sys_stats.sample()      # 每秒调一次
"""

import os
import time

# ---- 上一次 CPU 采样 (用于差分) ----
_prev = {"total": 0, "idle": 0, "t": 0.0}
# ---- 温度缓存 (温度变化慢, 不用每秒读) ----
_last_temp = {"t": 0.0, "v": None}

_CLK_TCK = 100.0        # ARM Linux 通常是 100


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _read_int(path):
    v = _read(path)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def cpu_percent():
    """返回 (总体占用%, 各核列表)。基于 /proc/stat 差分。"""
    txt = _read("/proc/stat")
    if not txt:
        return None, []
    cores = []
    total_all = idle_all = 0
    for line in txt.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if parts[0] == "cpu":
            nums = [int(x) for x in parts[1:11]]
        elif parts[0][3:].isdigit():
            nums = [int(x) for x in parts[1:11]]
            cores.append(nums)
        else:
            continue
        # user nice system idle iowait irq softirq steal guest guest_nice
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        tot = sum(nums)
        if parts[0] == "cpu":
            total_all, idle_all = tot, idle
    now = time.time()
    pct = None
    if _prev["total"] and total_all > _prev["total"]:
        dt = total_all - _prev["total"]
        di = idle_all - _prev["idle"]
        if dt > 0:
            pct = round(100.0 * (dt - di) / dt, 1)
    _prev.update(total=total_all, idle=idle_all, t=now)
    return pct, cores


def mem_info():
    """/proc/meminfo -> 总/可用/已用 (KB 和 %)。"""
    txt = _read("/proc/meminfo")
    if not txt:
        return None
    d = {}
    for line in txt.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip().split()[0]
            try:
                d[k] = int(v)
            except ValueError:
                pass
    total = d.get("MemTotal")
    if not total:
        return None
    avail = d.get("MemAvailable")
    if avail is None:
        avail = d.get("MemFree", 0) + d.get("Buffers", 0) + d.get("Cached", 0)
    used = total - avail
    return {
        "total_kb": total,
        "avail_kb": avail,
        "used_kb": used,
        "used_pct": round(100.0 * used / total, 1),
        "total_mb": round(total / 1024.0, 1),
        "used_mb": round(used / 1024.0, 1),
        "avail_mb": round(avail / 1024.0, 1),
    }


def loadavg():
    txt = _read("/proc/loadavg")
    if not txt:
        return None
    p = txt.split()
    try:
        return {"l1": float(p[0]), "l5": float(p[1]), "l15": float(p[2])}
    except (ValueError, IndexError):
        return None


def uptime_s():
    v = _read("/proc/uptime")
    if not v:
        return None
    try:
        return int(float(v.split()[0]))
    except (ValueError, IndexError):
        return None


def temp_c():
    """SoC 温度。变化慢, 缓存 5 秒 (读 thermal 是 sysfs, 不贵但没必要每秒)。"""
    now = time.time()
    # 只有"缓存有效 **且** 上次真的读到了值"才复用。
    # (早先写成 `_last_temp["v"] is not None` 才复用, 但如果第一次读失败,
    #  会一直返回 None 而不重试; 现在改成: 缓存过期就重读, 读到就更新。)
    if (now - _last_temp["t"] < 5.0) and _last_temp["v"] is not None:
        return _last_temp["v"]
    v = None
    for i in range(4):
        raw = _read_int("/sys/class/thermal/thermal_zone%d/temp" % i)
        if raw is not None and raw != 0:
            # 通常是毫摄氏度
            v = round(raw / 1000.0, 1) if raw > 1000 else float(raw)
            break
    # 读不到时不要把缓存时间推进 —— 否则会 5 秒不重试, 看起来像"卡住"
    if v is not None:
        _last_temp.update(t=now, v=v)
    else:
        _last_temp["t"] = 0.0
        _last_temp["v"] = None
    return v


def disk_info():
    """根分区和 SD 卡的可用空间 (statvfs, 很便宜)。"""
    out = {}
    for name, path in (("root", "/"), ("sd", "/mnt/sdcard"),
                       ("userdata", "/userdata")):
        try:
            s = os.statvfs(path)
            total = s.f_blocks * s.f_frsize
            free = s.f_bavail * s.f_frsize
            if total <= 0:
                continue
            out[name] = {
                "total_mb": round(total / 1048576.0, 1),
                "free_mb": round(free / 1048576.0, 1),
                "used_pct": round(100.0 * (total - free) / total, 1),
            }
        except Exception:
            pass
    return out or None


def npu_info():
    """NPU 状态。
    rknpu 的引用计数在 /sys/module/rknpu/refcnt (内核 5.10 常见)。
    拿不到就返回 loaded 但 in_use=None —— **不编数字**。
    """
    loaded = os.path.isdir("/sys/module/rknpu")
    dev = os.path.exists("/dev/rknpu")
    in_use = None
    for p in ("/sys/module/rknpu/refcnt",
              "/sys/module/rknpu/cores/0/refcnt"):
        v = _read_int(p)
        if v is not None:
            in_use = v > 0
            break
    return {"loaded": loaded, "dev": dev, "in_use": in_use}


def detect_info():
    """检测 (rkipc 的 enable_npu / npu_fps) 是否开着 + NPU 是否真被 rkipc 持有。

    这是本面板最有用的一项: 一眼看出"NPU 检测到底在不在跑"。
    """
    info = {"enabled": None, "fps": None, "holder": None}
    # 从 ini 读配置 (小文件, 便宜)
    txt = _read("/userdata/rkipc.ini")
    if txt:
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("enable_npu"):
                try:
                    info["enabled"] = int(line.split("=")[1].split(";")[0].strip()) != 0
                except (ValueError, IndexError):
                    pass
            elif line.startswith("npu_fps"):
                try:
                    info["fps"] = int(line.split("=")[1].split(";")[0].strip())
                except (ValueError, IndexError):
                    pass
    # 谁持有 /dev/rknpu (rkipc 应该持有)
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fd_dir = "/proc/%s/fd" % pid
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        tgt = os.readlink(os.path.join(fd_dir, fd))
                    except OSError:
                        continue
                    if tgt == "/dev/rknpu":
                        try:
                            with open("/proc/%s/comm" % pid) as f:
                                info["holder"] = f.read().strip()
                        except Exception:
                            info["holder"] = "pid %s" % pid
                        break
                if info["holder"]:
                    break
            except OSError:
                continue
    except OSError:
        pass
    return info


def processes():
    """关键进程是否在跑。

    ⚠️ 踩过的坑: 一开始只读 /proc/<pid>/comm, 但 **web_server.py 的 comm 是
    "python3"**(comm 是线程名, 不是脚本名), 所以面板上"网页"那一项始终是空的。
    改成: comm 匹配不到时, 再读一次 /proc/<pid>/cmdline 看脚本名。
    cmdline 只在少数未命中时读, 开销可以忽略。
    """
    want = ("rkipc", "mediamtx", "web_server", "python3")
    found = {}
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            nm = None
            try:
                with open("/proc/%s/comm" % pid) as f:
                    nm = f.read().strip()
            except OSError:
                continue
            if nm not in want:
                continue
            # python3 这种通用名字要再看 cmdline 才能区分是哪个脚本
            if nm.startswith("python"):
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        parts = f.read().split(b"\0")
                    for p in parts:
                        ps = p.decode("utf-8", "replace")
                        if ps.endswith(".py"):
                            nm = os.path.basename(ps)
                            break
                except OSError:
                    pass
            found[nm] = found.get(nm, 0) + 1
    except OSError:
        pass
    return found


# ---- 线程级 CPU 热点定位 (2026-10-06 加) ----
# 用户报"按住方向键 CPU 到 80-90%"。全局 CPU 百分比只说明"忙", 不能说明
# **谁在忙**。load average 11.99 在单核板上意味着有十几个任务在抢 CPU,
# 但那是历史平均, 也不能定位。
#
# 这里用 /proc/<pid>/task/*/stat 的 utime+stime 做**差分**, 直接算出每个
# 线程在这一秒里吃掉了多少 CPU, 按占用排序。答案会自己浮出来, 不用猜。
_prev_thr = {}


def thread_cpu():
    """返回本进程各线程的 CPU 占用 (按 % 降序)。

    单位是"占单核的百分比"。100% 表示这个线程吃满一个核。
    第一次调用没有基准, 返回空 (差分需要两次采样)。
    """
    global _prev_thr
    pids = set()
    try:
        me = os.getpid()
        pids.add(me)
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            nm = None
            try:
                with open("/proc/%s/comm" % pid) as f:
                    nm = f.read().strip()
            except OSError:
                continue
            if nm in ("rkipc", "mediamtx", "python3", "spitund"):
                pids.add(int(pid))
    except OSError:
        pass

    now = time.time()
    cur = {}
    for pid in pids:
        tdir = "/proc/%d/task" % pid
        try:
            tids = os.listdir(tdir)
        except OSError:
            continue
        for tid in tids:
            try:
                with open("%s/%s/stat" % (tdir, tid)) as f:
                    raw = f.read()
            except OSError:
                continue
            # comm 里可能有空格/括号, 取最后一个 ')' 之后才是字段
            rp = raw.rfind(")")
            if rp < 0:
                continue
            name = raw[raw.find("(") + 1:rp]
            flds = raw[rp + 2:].split()
            # 去掉 state(0) 后, utime 是第 11 个字段 => flds[11]
            try:
                ut, st = int(flds[11]), int(flds[12])
            except (IndexError, ValueError):
                continue
            key = (pid, tid)
            cur[key] = (name, ut + st)

    out = []
    hz = 100.0
    dt = 1.0
    if _prev_thr:
        # 用实际的采样间隔换算, 避免 1Hz 定时漂移导致虚高
        dt = max(0.2, now - _prev_thr.get("__t__", now))
    for key, (name, ticks) in cur.items():
        prev = _prev_thr.get(key)
        if not prev:
            continue
        d = ticks - prev[1]
        if d <= 0:
            continue
        pct = 100.0 * d / hz / dt
        if pct < 0.5:
            continue
        out.append({"pid": key[0], "tid": key[1], "name": name,
                    "pct": round(pct, 1)})
    out.sort(key=lambda x: -x["pct"])
    _prev_thr = dict(cur)
    _prev_thr["__t__"] = now
    return out[:8]


def sample():
    """一次性采集全部系统状态。每秒调一次, 必须便宜。"""
    pct, cores = cpu_percent()
    return {
        "cpu": {"pct": pct, "cores": len(cores)},
        "mem": mem_info(),
        "load": loadavg(),
        "uptime_s": uptime_s(),
        "temp_c": temp_c(),
        "disk": disk_info(),
        "npu": npu_info(),
        "detect": detect_info(),
        "proc": processes(),
        "threads": thread_cpu(),
    }


if __name__ == "__main__":
    import json
    import sys
    time.sleep(0.3)
    s = sample()
    time.sleep(1.0)
    s2 = sample()          # 第二次 CPU 才有差分值
    print(json.dumps(s2, ensure_ascii=False, indent=1))
    sys.exit(0)

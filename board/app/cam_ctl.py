import subprocess
import threading
import time

SUBDEV = "/dev/v4l-subdev2"

# 进程内缓存, 后台线程刷新
#
# exposure/gain 是"硬件现在的值"(每次 cam_read 刷新)。
# target_exposure/target_gain 是"用户要的值"——两者必须分开存。
#
# 为什么必须分开 (踩过的坑):
#   原来只有一个字段。cam_loop 每 0.5 秒 cam_read() 一次, 把硬件当前值
#   写回 CAM["exposure"], 然后再把这个值写回硬件。而 AE 早就把硬件改成
#   它自己的值了 —— 于是"锁定"每轮都在锁 AE 的新值, 完全无效: 用户设
#   曝光 500, 800ms 后读回来又是 408。
#
#   根因: 这个 subdev 没有 auto_exposure/exposure_auto 控制 (实测 v4l2-ctl -l
#   只有 exposure / analogue_gain 等), 自动曝光跑在 rkaiq 用户态 ISP 里,
#   V4L2 层关不掉。所以只能靠"持续重写用户要的值"来压制它 —— 那就必须
#   有一个不被 cam_read 覆盖的目标值。
CAM = {
    "exposure": None,
    "exposure_min": 1,
    "exposure_max": 1624,
    "gain": None,
    "gain_min": 128,
    "gain_max": 99614,
    "target_exposure": None,
    "target_gain": None,
    "lock": False,
    "ok": False,
    "err": None,
}
_LOCK = threading.Lock()


def _v4l2(*args, timeout=5):
    try:
        r = subprocess.run(["v4l2-ctl", "-d", SUBDEV] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)


def _parse(text, name):
    import re
    m = re.search(r"^\s*%s\s+0x[0-9a-f]+\s+\(int\)\s*:\s*(.*)$" % name,
                  text, re.M)
    if not m:
        return None
    body = m.group(1)
    out = {}
    for key in ("min", "max", "step", "default", "value"):
        mm = re.search(r"%s=(-?\d+)" % key, body)
        if mm:
            out[key] = int(mm.group(1))
    return out or None


def cam_read():
    ok, out = _v4l2("-l")
    if not ok:
        with _LOCK:
            CAM["ok"] = False
            CAM["err"] = out[:200]
        return dict(CAM)
    ex = _parse(out, "exposure")
    gn = _parse(out, "analogue_gain")
    with _LOCK:
        CAM["ok"] = True
        CAM["err"] = None
        if ex:
            CAM["exposure"] = ex.get("value")
            CAM["exposure_min"] = ex.get("min", CAM["exposure_min"])
            CAM["exposure_max"] = ex.get("max", CAM["exposure_max"])
        if gn:
            CAM["gain"] = gn.get("value")
            CAM["gain_min"] = gn.get("min", CAM["gain_min"])
            CAM["gain_max"] = gn.get("max", CAM["gain_max"])
        return dict(CAM)


def cam_set(exposure=None, gain=None, lock=None):
    """写曝光/增益。返回 (ok, msg)。立即生效, 不需要重启 rkipc。

    设了曝光或增益就**自动进入锁定**: 这个 subdev 没有 auto_exposure 控制,
    AE 在 rkaiq 里跑, 不持续重写就会被它立刻覆盖 (实测设 500, 800ms 后
    变回 408)。所以"手动设值"和"锁定"在这块板子上本来就是同一件事,
    不能指望用户再去勾一个复选框。
    """
    msgs = []
    if lock is not None:
        with _LOCK:
            CAM["lock"] = bool(lock)
            if not CAM["lock"]:
                # 解锁 = 交还给自动曝光, 清掉目标值
                CAM["target_exposure"] = None
                CAM["target_gain"] = None
        msgs.append("锁定=%s" % ("开" if CAM["lock"] else "关"))
    if exposure is not None:
        with _LOCK:
            lo, hi = CAM["exposure_min"], CAM["exposure_max"]
            v = max(lo, min(hi, int(exposure)))
            CAM["lock"] = True
            CAM["target_exposure"] = v
        ok, out = _v4l2("-c", "exposure=%d" % v)
        with _LOCK:
            if ok:
                CAM["exposure"] = v
        msgs.append("曝光=%d%s" % (v, "" if ok else " 失败"))
        if not ok:
            msgs.append(out[:120])
    if gain is not None:
        with _LOCK:
            lo, hi = CAM["gain_min"], CAM["gain_max"]
            v = max(lo, min(hi, int(gain)))
            CAM["lock"] = True
            CAM["target_gain"] = v
        ok, out = _v4l2("-c", "analogue_gain=%d" % v)
        with _LOCK:
            if ok:
                CAM["gain"] = v
        msgs.append("增益=%d%s" % (v, "" if ok else " 失败"))
        if not ok:
            msgs.append(out[:120])
    return True, ", ".join(msgs)


def cam_loop():
    """轮询当前值; 锁定期间持续重写**用户目标值**, 压制 rkaiq 的 AE。

    顺序很重要: 先重写目标值, 再 cam_read() 刷新显示。
    反过来的话 cam_read 会把 AE 的值写进 CAM, 下一轮就锁到 AE 的值上
    (这正是修复前的 bug)。重写用的是 target_*, 不受 cam_read 影响。
    """
    while True:
        time.sleep(0.5)
        try:
            with _LOCK:
                locked = CAM["lock"]
                tex, tgn = CAM["target_exposure"], CAM["target_gain"]
            if locked:
                if tex is not None:
                    _v4l2("-c", "exposure=%d" % tex)
                if tgn is not None:
                    _v4l2("-c", "analogue_gain=%d" % tgn)
            cam_read()
        except Exception:
            pass

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Luckfox 遥控车 — 板载 Web 服务器 (零依赖, 只用标准库)

特点:
  * 直接驱动电机 (不经 MQTT, 局域网延迟 <10ms)
  * 提供控制页面 + REST API
  * 图传复用板载 mediamtx (局域网 WebRTC, 已是现成服务)
  * 不依赖任何外部服务器

API:
  GET  /                  控制页面
  GET  /api/status        遥测 JSON (电池/GPS/4G/状态)
  POST /api/move          连续向量: {"vx":..,"vy":..,"w":..,"s":..}
  POST /api/cmd           离散指令: {"c":"forward|backward|stop|brake",..}
  POST /api/speed         设速度: {"s":70}

用法:
  python3 web_server.py [端口]     # 默认 80
"""
import json, os, subprocess, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 复用现有控制器代码 ----
from car_motor import FourMotor
from cam_ctl import cam_read, cam_set, cam_loop
from video_ctl import (video_info, video_switch,
                       exposure_info, exposure_switch,
                       quality_info, quality_set)

CONF_PATH = "/userdata/car/car_config.json"
INDEX = "/userdata/car/index.html"
ADC_GLOB = "/sys/bus/iio/devices/iio:device*/in_voltage%d_raw"
ADC_SCALE_MV = 1.7578125
# 过采样次数。取中位数需要足够样本才能压住偶发尖峰。
#
# 实测 (sysfs 读取速度约 1600 次/秒):
#     48 次  -> 约 30ms,  噪声跨度 67~122 LSB
#     400 次 -> 约 250ms, 噪声降到约 1/2.9
#
# ⚠️ 2026-10-05 从 400 降到 41。原因是一段**错误的历史判断**:
#    这里原来写着 "遥测线程是 1Hz, 250ms 只占 25% 周期, 而且它在后台跑,
#    不影响控制指令的延迟, 所以取 400"。
#    那句话在"1Hz"的前提下勉强成立, 但它掩盖了一个事实 ——
#    单核 240MHz 的板子上, **25% CPU 是很大的一笔开销**;
#    更要命的是它让"把循环提到 5Hz"看起来是免费的。
#
#    结果我为了做失控保护把循环改成 5Hz, 264ms × 5 = 132% CPU,
#    CPU 被 100% 占满、Web 线程拿不到时间片, 用户直接感觉到"控制延迟很高"。
#
#    41 次取中位数对偶发尖峰已经完全够用 (中位数只要求"尖峰不占多数"),
#    开销降到约 1/10。电池电压是缓变量, 1Hz 刷新 + 41 次过采样绰绰有余。
ADC_OVERSAMPLE = 41
ADC_TRIM = 16                # 已改用中位数, 保留常量仅为兼容旧配置
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 80

# The ESP32-C3 WiFi bridge. It is the only route to the board with no cable, so
# the web UI reports its link state. It answers UDP "stat" with JSON.
C3_HOST = "192.168.3.69"
C3_STAT_PORT = 10000

# ---------------- 配置 ----------------
def load_conf():
    try:
        return json.load(open(CONF_PATH))
    except Exception as e:
        print("[conf] 读取失败, 用默认:", e)
        return {"pin": {"STBY": 7,
                        "FL": {"IN1": 65, "IN2": 64, "PWM": 67},
                        "FR": {"IN1": 54, "IN2": 55, "PWM": 58},
                        "BL": {"IN1": 49, "IN2": 50, "PWM": 51},
                        "BR": {"IN1": 59, "IN2": 41, "PWM": 48}},
                "telemetry": {"adc_channel": 1, "adc_ratio": 8.71}}

CONF = load_conf()
TEL = CONF.get("telemetry", {})
ADC_CH = TEL.get("adc_channel", 1)
ADC_RATIO = float(TEL.get("adc_ratio", 8.71))

# ---------------- 电池电压换算 (标定曲线) ----------------
#
# 为什么不是简单的 "mv * 比值":
#
# 分压比实测**不是常数**, 而是随电压上升:
#     低压端 (raw 768~777, 约 11.2~11.3V)  分压比 8.29 ~ 8.32
#     高压端 (raw 797,       约 12.26V)     分压比 8.75
#
# 单个比值无法同时满足两端 —— 用 8.71 时低压端偏高 0.5V (用户最早报的
# "电压不准 11.69 / 网页 12.2V" 就是这个), 用 8.30 时高压端偏低 0.63V。
#
# 下面是用**万用表**实测的 4 个点做的最小二乘二次拟合
# (BB 响的数据已全部剔除, 它的精度和测量时刻都不可靠):
#
#     raw   实测V    曲线    残差
#     768   11.23   11.220   -0.010
#     769   11.21   11.222   +0.012
#     777   11.33   11.328   -0.002
#     797   12.26   12.260    0.000
#
#     二次拟合 RMS = 0.008V, 最大残差 0.012V
#     (对比: 直线拟合 RMS 0.089V —— 曲线比直线好 11 倍, 证明确实有曲率)
#
# 标定点存在 /userdata/bat_cal.json, 以后补点可以用 _batcal.py 重新拟合。
BAT_COEF = (712.795469286, -1.82835728943, 0.00119120707926)  # a0 + a1*raw + a2*raw^2
BAT_RAW_MIN = 700      # 曲线有效区间下限 (低于此值属于外推, 回退到比值法)
BAT_RAW_MAX = 860      # 上限


def bat_volts(mv):
    """引脚毫伏 -> 电池电压。优先用标定曲线, 超出标定范围则回退比值法。

    回退是必要的: 曲线只在 raw 700~860 之间验证过, 电池更低压时
    (比如 10.5V 以下) 二次项会外推出离谱的值, 不如用线性比值稳妥。
    """
    if not mv:
        return None
    raw = mv / ADC_SCALE_MV
    if BAT_RAW_MIN <= raw <= BAT_RAW_MAX:
        a0, a1, a2 = BAT_COEF
        return a0 + a1 * raw + a2 * raw * raw
    return mv * ADC_RATIO / 1000.0


# ---------------- 电机 ----------------
motor = None
MOTOR_LOCK = threading.Lock()
STATE = {"dir": "stop", "speed": 30, "vx": 0.0, "vy": 0.0, "w": 0.0, "ts": time.time()}

def init_motor():
    global motor
    try:
        motor = FourMotor(CONF["pin"], simulate=False)
        motor.start_pwm()
        print("[motor] 已启动 (真实电机模式)")
    except Exception as e:
        print("[motor] 启动失败:", e)
        motor = None

# ---------------- 遥测 ----------------
def read_adc_mv(ch):
    """过采样读 ADC, 返回毫伏.

    用中位数而非截尾均值。实测 300 个样本的 spread 高达 100 LSB
    (引脚 176mV / 电池约 1.6V), 说明存在偶发的大幅尖峰; 截尾均值在这种
    分布下仍会被拖动, 而中位数完全不受尾部影响。
    """
    import glob
    for p in glob.glob(ADC_GLOB % ch):
        vals = []
        try:
            f = open(p)
        except Exception:
            continue
        for _ in range(ADC_OVERSAMPLE):
            try:
                f.seek(0)
                vals.append(int(f.read().strip()))
            except Exception:
                pass
        f.close()
        if not vals:
            continue
        vals.sort()
        n = len(vals)
        raw = vals[n // 2] if (n % 2) else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
        if raw >= 1022:          # 悬空/满量程视为未接
            return None
        return raw * ADC_SCALE_MV
    return None

_gps = {"present": os.path.exists("/dev/ttyS4"), "fix": False,
        "lat": None, "lon": None, "sats": None, "speed_kmh": None,
        "state": "init", "snr": None, "seen": 0, "bad": 0, "age": None}


def _nmea_ok(s):
    """校验 NMEA 校验和。

    实测串口有帧错误 (dmesg fe:4), 数据流里会出现被污染的语句, 例如
        '$GN$GNGGA,025921.000,,,,,0,00,25.5,,,,,,*77'
    里面混进了 '$GN' 前缀。不做校验就会把垃圾当成有效定位数据。
    没有 '*' 的语句一律丢弃。
    """
    i = s.rfind("*")
    if i < 0 or i + 3 > len(s):
        return False
    try:
        want = int(s[i + 1:i + 3], 16)
    except ValueError:
        return False
    got = 0
    for ch in s[1:i]:
        got ^= ord(ch)
    return got == want


def _dm(v, h):
    """NMEA 的 ddmm.mmmm 转十进制度。"""
    x = float(v)
    d = int(x // 100)
    r = d + (x - d * 100) / 60.0
    return round(-r if h in ("S", "W") else r, 6)


def gps_reader():
    """后台读 NMEA。

    时序: 9600 8N1, 每 1 秒一组 $GNGGA/$GNRMC/$GNGLL/$GPGSV...

    之前只认 '$GPGGA'/'$GNGGA' 两个前缀, 而实际模块发的是 GN (多星座合并)
    + GP (GPS) + BD (北斗) 混发, 且有 '$GNGLL' 也带经纬度。现在按语句类型
    判断, 不依赖 talker 前缀, 稳得多。
    """
    dev = "/dev/ttyS4"
    if not os.path.exists(dev):
        return
    import termios
    fd = None
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        a = termios.tcgetattr(fd)
        a[0] = a[1] = 0
        a[2] = termios.B9600 | termios.CS8 | termios.CLOCAL | termios.CREAD
        a[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        buf = b""
        t_last = time.time()
        while True:
            try:
                c = os.read(fd, 512)
            except BlockingIOError:
                time.sleep(0.2)
                c = b""
            except OSError:
                break
            if c:
                t_last = time.time()
                buf += c
            # 每秒刷新一次"多久没收到数据", 供网页显示
            if time.time() - t_last > 1.0:
                _gps["age"] = round(time.time() - t_last, 1)
                if time.time() - t_last > 5.0:
                    _gps["present"] = False
                    _gps["state"] = "no_data"
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("ascii", "ignore").strip()
                if not s.startswith("$"):
                    continue
                _gps["seen"] += 1
                if not _nmea_ok(s):
                    _gps["bad"] += 1
                    continue
                _gps["age"] = 0.0
                _gps["present"] = True
                # 去掉校验和字段后再切分
                body = s[1:s.rfind("*")]
                f = body.split(",")
                kind = f[0][2:] if len(f[0]) > 2 else ""
                try:
                    if kind == "GGA":
                        # $--GGA,time,lat,N,lon,E,quality,numSV,HDOP,alt,...
                        if len(f) > 9:
                            _gps["sats"] = int(f[7]) if f[7] else 0
                            q = f[6] or "0"
                            _gps["fix"] = q in ("1", "2", "4", "5")
                            if f[2] and f[4]:
                                _gps["lat"] = _dm(f[2], f[3])
                                _gps["lon"] = _dm(f[4], f[5])
                            if f[8]:
                                _gps["hdop"] = float(f[8])
                    elif kind in ("RMC",):
                        # $--RMC,time,status,lat,N,lon,E,speed,course,date
                        if len(f) > 8:
                            if f[2] == "A":
                                _gps["fix"] = True
                                if f[3] and f[5]:
                                    _gps["lat"] = _dm(f[3], f[4])
                                    _gps["lon"] = _dm(f[5], f[6])
                                if f[7]:
                                    _gps["speed_kmh"] = round(
                                        float(f[7]) * 1.852, 1)
                            else:
                                # status V = 无效定位。不要因此清掉 sats,
                                # GGA 里的卫星数更有信息量。
                                _gps["fix"] = False
                    elif kind == "GLL":
                        # $--GLL,lat,N,lon,E,time,status -- 只在有定位时才有值
                        if len(f) > 6 and f[6] == "A" and f[1] and f[3]:
                            _gps["lat"] = _dm(f[1], f[2])
                            _gps["lon"] = _dm(f[3], f[4])
                            _gps["fix"] = True
                    elif kind == "GSV":
                        # $--GSV,total,idx,num,sat...,[snr]...
                        # 有 C/N0 的卫星才是有用的: 模块"看得到"但 C/N0 全空
                        # 表示信号太弱, 解不出星历 -- 这正是搜不到星的特征。
                        if len(f) >= 4:
                            try:
                                msg_n = int(f[1] or 0)
                            except ValueError:
                                msg_n = 0
                            best = _gps.get("snr") or 0
                            # 每 4 个字段一颗星: prn,elev,azim,cn0
                            for i in range(4, len(f) - 3, 4):
                                if i + 3 < len(f) and f[i + 3]:
                                    try:
                                        v = float(f[i + 3])
                                        if v > best:
                                            best = v
                                    except ValueError:
                                        pass
                            _gps["snr"] = best if best else None
                            # 一轮 GSV 结束, 记录"看得到几颗"
                            if not msg_n or f[2] == f[1]:
                                pass
                except Exception:
                    pass
    except Exception as e:
        print("[gps]", e)
    finally:
        if fd is not None:
            try: os.close(fd)
            except Exception: pass

def check_4g():
    import glob
    try:
        for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            try:
                if open(d).read().strip().lower() == "2c7c":
                    b = os.path.dirname(d)
                    prod = ""
                    try: prod = open(os.path.join(b, "product")).read().strip()
                    except Exception: pass
                    return {"present": True, "module": prod}
            except Exception:
                pass
    except Exception:
        pass
    return {"present": False, "module": None}

def net_info():
    """当前 IP / 接口"""
    out = {}
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); out["ip"] = s.getsockname()[0]; s.close()
    except Exception:
        out["ip"] = None
    ifs = []
    try:
        for n in sorted(os.listdir("/sys/class/net")):
            if n == "lo":
                continue
            try:
                st = open("/sys/class/net/%s/operstate" % n).read().strip()
            except Exception:
                st = "?"
            ifs.append({"name": n, "state": st})
    except Exception:
        pass
    out["ifaces"] = ifs
    return out

def c3_status():
    """Read the C5's own state, pushed over the SPI tunnel.

    ---- 为什么有两个数据源 ----
    隧道现在跑在**内核模块**里 (spitun.ko), 它不再写 /tmp/c3_state.json ——
    那个文件是用户态 spinet.py 时代的产物。驱动把 C5 的状态帧留在内存里,
    通过 sysfs 暴露出来:
        /sys/class/net/spitun0/c3_status

    所以先读 sysfs, 读不到再退回旧的 json 文件 —— 这样两种情况都能工作:
      * 新固件 (内核模块): 走 sysfs
      * 万一回退到用户态隧道: 走 json

    注意板子和 C5 之间**没有 IP 链路** (隧道只转发经调度的 IP 报文),
    板子不能直接 UDP 查询 C5, 只能等 C5 主动推状态帧过来。
    """
    raw = None

    # 1) 内核模块的 sysfs (当前方案)
    try:
        with open("/sys/class/net/spitun0/c3_status") as f:
            txt = f.read().strip()
        if txt:
            raw = json.loads(txt)
    except Exception:
        raw = None

    # 2) 用户态隧道的 json 文件 (兜底)
    if raw is None:
        try:
            with open("/tmp/c3_state.json") as f:
                raw = json.load(f)
        except Exception:
            return {"online": False}

    try:
        d = dict(raw)
        r = d.get("rssi", 0)
        if r == 0:            d["quality"] = "未知"
        elif r > -60:         d["quality"] = "优"
        elif r > -70:         d["quality"] = "良"
        elif r > -80:         d["quality"] = "弱"
        else:                 d["quality"] = "很差"
        d["online"] = True
        d["heap_free"] = d.get("free")
        d["uptime_s"] = round(d.get("up_ms", 0) / 1000.0, 1)
        return d
    except Exception:
        return {"online": False}

# ---------------- 坐标系转换 ----------------
#
# GPS 模块输出的是 WGS-84 (国际标准)。中国法律要求公开地图服务使用加密后的
# GCJ-02 (俗称"火星坐标系"), 百度再用自己的 BD-09。三者互不相同, 在大连一带
# WGS-84 和 BD-09 差了约 1.3 公里 —— 直接把 GPS 原始值标到百度地图上, 落点
# 会偏出去一公里多, 看起来像"GPS 不准", 其实是坐标系没换。
#
# 所以: 保留一份原始 WGS-84 (给需要精确值的地方), 再算好 GCJ-02 和 BD-09
# 给地图链接用。转换在国外必须跳过 —— 算法只在中国境内有效, 套用到境外反而
# 会把正确的坐标算歪。
PI = 3.1415926535897932384626
GCJ_A = 6378245.0                      # 克拉索夫斯基椭球
GCJ_EE = 0.00669342162296594323


def _out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tlat(x, y):
    import math
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    import math
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lat, lon):
    """GPS 原始值 -> 高德/腾讯/谷歌中国 用的坐标。"""
    import math
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    rl = lat / 180.0 * PI
    m = math.sin(rl)
    m = 1 - GCJ_EE * m * m
    sm = math.sqrt(m)
    dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (m * sm) * PI)
    dlon = (dlon * 180.0) / (GCJ_A / sm * math.cos(rl) * PI)
    return round(lat + dlat, 6), round(lon + dlon, 6)


def gcj02_to_bd09(lat, lon):
    """GCJ-02 -> 百度 BD-09。"""
    import math
    x, y = lon, lat
    z = math.sqrt(x * x + y * y) + 0.00002 * math.sin(y * PI * 3000.0 / 180.0)
    th = math.atan2(y, x) + 0.000003 * math.cos(x * PI * 3000.0 / 180.0)
    return round(z * math.sin(th) + 0.006, 6), round(z * math.cos(th) + 0.0065, 6)


def wgs84_to_bd09(lat, lon):
    """GPS 原始值 -> 百度地图用的坐标 (两步)。"""
    g = wgs84_to_gcj02(lat, lon)
    return gcj02_to_bd09(*g)


def gps_state(g):
    """把原始字段归纳成一句人话。

    这个函数存在的理由: "GPS 没定位"有四种完全不同的原因, 而它们在网页上
    原来长得一模一样。必须区分开, 否则用户只能靠猜:

      no_data  串口没数据   -> 没接 / 线断 / 波特率错
      searching 有数据没星  -> 天线没接好 / 全被遮挡
      weak     看得见但弱   -> 信号太差, 解不出星历 (室内典型)
      nofix    信号够但没锁 -> 正常冷启动中, 再等
      fix      已定位
    """
    if not g.get("present"):
        return "no_data", "GPS 无数据 (检查接线)"
    if g.get("age") is not None and g["age"] > 5:
        return "no_data", "GPS 数据中断 %.0fs" % g["age"]
    if g.get("fix") and g.get("lat") is not None:
        return "fix", "已定位"
    sats = g.get("sats") or 0
    snr = g.get("snr")
    if sats == 0 and not snr:
        return "searching", "搜星中 (还没看到卫星)"
    if sats == 0 and snr:
        return "weak", "看到卫星但信号弱 (C/N0 %d, 需 >30)" % int(snr)
    if g.get("fix") and g.get("lat") is None:
        return "nofix", "定位中, 等经纬度"
    return "nofix", "定位中 (%d 星)" % sats


def _page_ver():
    """index.html 的版本标识 (mtime 秒)。页面用它检测"文件已被更新"。"""
    try:
        return int(os.path.getmtime(INDEX))
    except Exception:
        return 0


def build_status():
    # Served from the cache that telemetry_loop() refreshes once a second.
    #
    # Measured on the board, these calls dominated EVERY request:
    #   read_adc_mv (16x oversample)  ~19.6 ms
    #   check_4g    (usb sysfs glob)  ~16.6 ms
    #   net_info    (UDP connect)      ~1.1 ms
    # which is why /api/status cost ~73ms on loopback while serving the 22KB
    # index.html cost only ~18ms. Battery voltage and modem presence change on
    # the order of seconds, so recomputing them per request bought nothing.
    now = time.time()
    with TEL_LOCK:
        mv = _tel["mv"]
        gps = dict(_tel["gps"])
        net4g = dict(_tel["net4g"])
        net = dict(_tel["net"])
        c3 = dict(_tel["c3"])
    # 给网页一句人话解释为什么没有定位 (见 gps_state 的说明)
    gps["state"], gps["state_text"] = gps_state(gps)
    # 顺带把三种坐标都算好。原始 lat/lon 保持 WGS-84 不动 (数据要干净),
    # 另外两个只给地图链接用, 避免网页再犯"拿 WGS 喂百度"的错。
    if gps.get("lat") is not None and gps.get("lon") is not None:
        try:
            glat, glon = wgs84_to_gcj02(gps["lat"], gps["lon"])
            blat, blon = gcj02_to_bd09(glat, glon)
            gps["gcj_lat"], gps["gcj_lon"] = glat, glon
            gps["bd_lat"], gps["bd_lon"] = blat, blon
            gps["in_china"] = not _out_of_china(gps["lat"], gps["lon"])
        except Exception:
            pass
    with MOTOR_LOCK:
        st = dict(STATE)
    return {
        "mode": "live" if motor else "sim",
        "dir": st["dir"], "speed": st["speed"],
        "vx": st["vx"], "vy": st["vy"], "w": st["w"],
        "ts": int(now), "online": True,
        # 失控保护触发次数。正常情况下应该是 0 或在断网/关页面时缓慢增加。
        # 如果**行驶中**这个数一直在涨, 说明心跳没送达 (网页那边有问题),
        # 值得马上查 —— 这正是"控制失控"的量化指标。
        "failsafe": st.get("failsafe", 0),
        # 距离最近一次指令过了多久 (秒)。>MOVETIMEOUT 就是在失控边缘。
        "cmd_age": round(now - st.get("ts", now), 2),
        # time.monotonic(): BOOT via time.time() survives NTP steps (board
        # clock starts ~2020 then jumps to real time — uptime showed ~5 years).
        "uptime": int(time.monotonic() - BOOT_MONO),
        # 失控保护现状 + "这个超时是不是太短"的定量指标
        "failsafe_cfg": _fs_stats(),

        # 页面版本 = index.html 的 mtime。
        #
        # ⚠️ 2026-10-05 加, 因为踩了一次真实的坑: 用户浏览器里那个页面是
        # **上午 10:43 打开的**, 而我在 18:42 才改好前端的控制循环。旧页面
        # 一直跑着旧 JS ("只在数值变化时才发指令"), 于是板子的 1 秒失控保护
        # 不停被触发 —— 用户看到的是"车走走停停/控制不灵", 而我从板子侧
        # 怎么查都是好的 (链路健康、C5 没重启)。
        #
        # 有了这个字段, 页面每 1 秒轮询时就能发现"文件已更新", 自己 reload,
        # 不用再指望用户手动刷新 —— 这类"改了没生效"的故障太容易误判。
        "ver": _page_ver(),
        "tel": {
            "bat_mv": mv,
            "bat_v": round(bat_volts(mv), 2) if mv else None,
            "adc_ratio": ADC_RATIO,
            "gps": gps,
            "net4g": net4g,
            "net": net,
            "c3": c3,
        },
    }

# ---------------- 遥测后台缓存 ----------------
TEL_LOCK = threading.Lock()
_tel = {
    "mv": None,
    "gps": dict(_gps),
    "net4g": {"present": False, "module": None},
    "net": {"ip": None, "ifaces": []},
    "c3": {"online": False},
}

def telemetry_loop():
    """重的遥测采集 —— **1Hz, 不要再提高**。

    ⚠️ 2026-10-05 踩过的坑 (自己造成的, 记下来):
    ---------------------------------------------------------------
    为了做失控保护, 我曾把这个循环的周期从 1.0s 改成 0.2s,
    想着"保护检查当然要跑得勤一点"。结果**整块板子被钉死**:

        load average: 12.99, 13.04, 12.98   (单核)
        CPU: 64% usr  35% sys  0% idle
        python3 (web_server) 约 57% CPU

    实测每个函数的单次耗时 (板上跑出来的):

        read_adc_mv    264.39 ms   ← !!!
        net_info        13.03 ms
        c3_status        3.55 ms
        check_4g         0.94 ms
        _failsafe_check  0.02 ms

    `read_adc_mv` 单次 264ms 是因为它要做 **ADC_OVERSAMPLE=400 次
    sysfs 读**再取中位数。1Hz 时占 26% CPU (勉强能忍), **5Hz 就是
    132% —— 物理上跑不完**, 于是 CPU 被 100% 占满, Web 线程拿不到
    时间片, 用户感觉到的就是"控制延迟很高"。

    结论: **重的采集和轻的保护必须分开两个循环。**
    保护检查 (`_failsafe_check`, 0.02ms) 放 failsafe_loop 跑 5Hz;
    这里的重活儿保持 1Hz。
    ---------------------------------------------------------------
    """
    while True:
        try:
            mv = read_adc_mv(ADC_CH)
        except Exception:
            mv = None
        try:
            g4 = check_4g()
        except Exception:
            g4 = {"present": False, "module": None}
        try:
            nw = net_info()
        except Exception:
            nw = {"ip": None, "ifaces": []}
        try:
            c3 = c3_status()
        except Exception:
            c3 = {"online": False}
        with TEL_LOCK:
            _tel["mv"] = mv
            _tel["gps"] = dict(_gps)
            _tel["net4g"] = g4
            _tel["net"] = nw
            _tel["c3"] = c3
        # 立刻再查一次保护, 不用等下一个 tick
        _failsafe_check()
        time.sleep(1.0)


def failsafe_loop():
    """失控保护专用循环 —— 5Hz, **只做时间戳比较, 绝不在这里做 IO**。

    `_failsafe_check()` 实测 0.02ms, 5Hz 下 CPU 占用可以忽略。
    它只读内存里的 STATE 和一个时间戳, 所以放在高频循环里是安全的。

    这个拆分的理由见 telemetry_loop 的注释: 曾经把重的采集也提到 5Hz,
    直接把板子 CPU 打满, 反而让"控制延迟"变得很高 —— 本末倒置。
    """
    while True:
        _failsafe_check()
        time.sleep(0.2)


# ============================================================================
# 失控保护 (failsafe / deadman switch)
# ============================================================================
# ⚠️ 安全关键逻辑。之前**完全没有**, 后果很严重:
#
#   网页端只在**数值变化**时才发指令 (见 index.html 的 `if(m !== lastMove)`),
#   按住摇杆不动时一条都不发, 板子就一直保持最后那组向量。于是只要出现
#   下面任何一种情况, **车会带着最后的指令一直跑, 用户松手也停不下来**:
#
#     * WiFi 抖动 / 隧道卡顿 (今天实测发生过内核卡死)
#     * 浏览器标签页失焦、被系统冻结、手机锁屏
#     * 页面崩溃 / 关掉 / 手机熄屏
#     * C5 重启 (刚刚就重启过一次)
#
#   这就是"控制失控"。
#
# 判据: 只要车在动, 而最近 MOVETIMEOUT 秒内**没有任何指令到达**, 就停车。
#
# 为什么两边必须配合改:
#   * 只改板子 -> 用户按住摇杆不动时会被误判成失联, 车走走停停。
#     所以 index.html 必须改成**按住期间持续发心跳** (见那里的说明)。
#   * 只改网页 -> 网页崩了 / 网断了 就完全没人管了, 保护等于没有。
#
# 超时可现场调 (2026-10-05 第 26 轮):
#   来源优先级: car_config.json 的 "failsafe_s" -> 默认值
#   也可以在运行时热切换:  POST /api/failsafe {"t": 0.5}
#
# ⚠️ 调这个数之前先看下面这段实测数据, 它决定"调短"是帮忙还是帮倒忙:
#
#   实测 (2026-10-05, PC 发 100ms 心跳, 150 条):
#     到达间隔  p50 109ms / p90 111ms / p99 130ms     <- 正常时很稳
#     但端到端 RTT  p50 42ms / p90 401ms / max 402ms  <- 每 4 条就有 1 条卡满
#
#   失控保护的判据是"距最后一条指令**到达**的时间"。上面那 25% 的 400ms 卡顿
#   意味着: 超时只要短于 ~0.4s, 那些卡顿就**全部**变成误停 ——
#   0.2s 会变成每秒停两三次, 车根本没法开。
#
#   所以: 调短**不会**降低延迟, 只会把"偶发卡顿"翻译成"频繁停车"。
#   真正要修的是那 400ms (见 PROGRESS 第二十七轮)。
#   本文件同时统计"间隔超过超时的次数/比例"(fs_would_stop / fs_over_pct),
#   用来定量判断当前这个超时是不是在误停 —— 别靠猜。
MOVETIMEOUT_DEFAULT = 1.0
MOVETIMEOUT = MOVETIMEOUT_DEFAULT


def _load_failsafe_timeout():
    """从 car_config.json 读 failsafe_s (没有就用默认)。"""
    global MOVETIMEOUT
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            v = json.load(f).get("failsafe_s")
        if v is not None:
            MOVETIMEOUT = max(0.1, min(10.0, float(v)))
    except Exception:
        pass
    return MOVETIMEOUT


def set_failsafe_timeout(t):
    """热切换失控超时。返回 (ok, 生效值)。"""
    global MOVETIMEOUT
    try:
        MOVETIMEOUT = max(0.1, min(10.0, float(t)))
        return True, MOVETIMEOUT
    except Exception:
        return False, MOVETIMEOUT


# ---- "这个超时是不是太短" 的定量观测 ----
# 每次指令到达时, 记录"距上一条的间隔"。间隔 > MOVETIMEOUT 的那些,
# 在"车在动"的情况下就会被误停 —— 这个比例就是误停率。
_fs = {"last_cmd": 0.0, "n": 0, "over": 0, "max_gap": 0.0, "gaps": []}
FS_GAP_SAMPLES = 120


def _fs_note_cmd():
    """每条控制指令到达时调用一次 (do_POST 里)。"""
    try:
        now = time.time()
        prev = _fs["last_cmd"]
        _fs["last_cmd"] = now
        if prev <= 0:
            return
        gap = now - prev
        # 只在"车在动"时才统计: 停着的时候本来就不发心跳, 间隔大是正常的
        with MOTOR_LOCK:
            moving = (abs(STATE.get("vx", 0.0)) > 0.02 or
                      abs(STATE.get("vy", 0.0)) > 0.02 or
                      abs(STATE.get("w", 0.0)) > 0.02 or
                      STATE.get("dir") not in _STILL_DIRS)
        if not moving and gap > 2.0:
            _fs["last_cmd"] = now
            return
        _fs["n"] += 1
        if gap > _fs["max_gap"]:
            _fs["max_gap"] = gap
        _fs["gaps"].append(gap)
        if len(_fs["gaps"]) > FS_GAP_SAMPLES:
            _fs["gaps"].pop(0)
        if gap > MOVETIMEOUT:
            _fs["over"] += 1
    except Exception:
        pass


def _fs_stats():
    g = sorted(_fs["gaps"])
    n = len(g)
    pct = (100.0 * _fs["over"] / _fs["n"]) if _fs["n"] else 0.0
    return {
        "timeout": round(MOVETIMEOUT, 2),
        "cmd_gaps": _fs["n"],                 # 统计了多少条指令间隔
        "over_timeout": _fs["over"],          # 其中超过超时的次数
        "over_pct": round(pct, 1),            # <<< 这个就是"误停率"
        "max_gap": round(_fs["max_gap"], 2),
        "gap_p50": round(g[n // 2], 3) if n else None,
        "gap_p90": round(g[int(n * 0.9)], 3) if n else None,
        "warning": ("失控超时过短, 正在误停 (间隔超时占 %.0f%%)" % pct)
                   if (pct > 5.0 and _fs["n"] > 20) else None,
    }

# 只有这两种状态算"停着", 不触发保护
_STILL_DIRS = ("stop", "brake")


def _failsafe_check():
    try:
        now = time.time()
        triggered = 0.0
        with MOTOR_LOCK:
            age = now - STATE.get("ts", now)
            moving = (
                abs(STATE.get("vx", 0.0)) > 0.02 or
                abs(STATE.get("vy", 0.0)) > 0.02 or
                abs(STATE.get("w", 0.0)) > 0.02 or
                STATE.get("dir") not in _STILL_DIRS
            )
            if not moving or age <= MOVETIMEOUT:
                return
            if motor is not None:
                motor.stop()
            STATE.update(dir="stop", vx=0.0, vy=0.0, w=0.0, ts=now)
            STATE["failsafe"] = int(STATE.get("failsafe", 0)) + 1
            triggered = age

        # 掉出 MOTOR_LOCK 再写日志, 避免持锁做 IO。
        # 注意用内置 open, 不用 io.open —— 本文件**没有 import io**
        # (只 import 了 json/os/sys/time/threading)。之前写成 io.open,
        # 抛 NameError 后被上面的 except 静默吞掉, 表现是"保护生效但日志不出"。
        try:
            with open("/userdata/failsafe.log", "a", encoding="utf-8") as f:
                f.write("%s 失联 %.2fs -> 停车\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), triggered))
        except Exception:
            pass
    except Exception:
        # 保护逻辑本身绝不能把 telemetry_loop 搞挂
        pass

# ============================================================================
# 回程路由 (2026-10-05): **为什么不在这一层修**
# ============================================================================
# 现象: 客户端 IP 一变 (DHCP 续约/换设备), 网页和控制就"完全没反应",
# 而板子侧看什么都是好的 —— 根因是回包没有回隧道的路由。
#
# 我一开始想在这里自愈: 每个请求进来时检查 client_ip 并补一条 /32 路由。
# **实测无效, 原因很硬**: TCP 的 SYN-ACK 是**内核**在 HTTP 处理器跑之前
# 就发出去的 —— 没有路由, 三次握手根本完不成, 处理器永远不会被调用,
# 自愈代码永远等不到执行 (实测: 删掉路由后 HTTP 直接超时, 日志里一行都没有)。
#
# 正确做法在**路由层** (/etc/init.d/S22spinet):
#     ip rule add from 10.77.0.2 lookup 100 pref 100
#     ip route replace 192.168.3.0/24 dev spitun0 src 10.77.0.2 table 100
# 板子经隧道发出的包源地址恒为 10.77.0.2, 于是**不需要知道客户端是谁**,
# DHCP 怎么变、换手机还是电脑, 都不会失联。
# ⚠️ 真实故障, 而且症状极具误导性:
#
#   板子的回程路由原来在 /etc/init.d/S22spinet 里**写死了一个客户端 IP**
#   (CLIENTS="192.168.3.64")。客户端一换 IP —— DHCP 租约到期、路由器重启、
#   手机重连、从电脑换到手机 —— 板子的回包就找不到回去的路, 从默认路由
#   (usb0/169.254) 发出去了, C5 的 NAPT 根本不认, 包直接消失。
#
#   现象: 网页/控制**完全没反应**, 但 C5 在线、隧道正常、板子 CPU 空闲、
#   本地 /api/move 只要 12ms —— 从板子侧看**一切都是好的**。
#   实测: 用户 PC 从 192.168.3.64 变成 192.168.3.65 之后, 控制就是"按了
#   没反应", 补上 /32 路由后**立刻恢复**。
#
# 自愈做法: 每个请求都看一下这条连接的**本地地址** ——
#   * 本地地址是 10.77.0.2  => 这个客户端是经 SPI 隧道来的, 回包必须回隧道
#   * 本地地址是 192.168.3.x => 网线直连, 用默认路由就行, 不要乱动
# 隧道来的客户端如果还没有 /32 路由, 立刻补一条 (ip route replace)。
# 这样 DHCP 变了也会自己长回来, 不需要任何人工干预。
# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = "LuckfoxCar/1.0"
    # HTTP/1.1 keep-alive: the browser reuses ONE connection for joystick
    # moves + status polls. Each new connection costs the C3 a reverse-proxy
    # slot with 18KB of buffers -- 20 moves/s churned slots fast enough to
    # exhaust the C3 heap and kill the video session (minfree 2.3KB).
    protocol_version = "HTTP/1.1"
    # 空闲连接超时。缺了这个, ThreadingHTTPServer 会为每条 TCP 连接留一个
    # 线程, 而 HTTP/1.1 的 keep-alive 让连接一直不关 —— 实测板上积了 14 个
    # 永不退出的处理器线程。它们在单核上争 GIL, 是控制延迟随时间变差的原因。
    # 浏览器每 1 秒轮询 /api/status, 连接始终活跃, 所以这个超时不会影响 UI。
    timeout = 15
    # 控制延迟的最大元凶, 实测数据 (板子自己打自己, 复用 keep-alive 连接):
    #
    #   Nagle 开(默认) 中位 49.99ms   <-- 就是这条
    #   Nagle 关       中位 13.89ms
    #   裸 socket 单次 send 中位  5.79ms
    #
    # 响应是分两次 write 出去的 (send_response 的头部一次, body 一次)。
    # 浏览器保持长连接, 第一个小包发出去后 Nagle 要等对端的延迟 ACK, 而对端
    # 此刻没有数据要回, 要等自己的延迟 ACK 定时器 (Linux 40ms)。于是每条
    # 指令都白白多付约 36ms。这跟无线信号、SPI 速度都无关, 纯是这一行缺了。
    #
    # disable_nagle_algorithm 是 socketserver.StreamRequestHandler 的官方
    # 开关, 置 True 后 setup() 会对该连接 setsockopt(TCP_NODELAY)。
    # 注意: 每次新建连接的测法看不出这个问题 (新连接不触发该路径), 必须用
    # 复用连接测 —— 浏览器用的正是复用连接。
    disable_nagle_algorithm = True

    def log_message(self, fmt, *a):
        pass  # 静音, 避免刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            try:
                html = open(INDEX, "rb").read()
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "index.html 读取失败: %s" % e, "text/plain; charset=utf-8")
        elif p == "/api/status":
            self._json(200, build_status())
        elif p == "/api/ping":
            self._json(200, {"ok": True, "ts": int(time.time())})
        elif p == "/api/cam":
            # 摄像头实时参数 (曝光/增益)。见 cam_ctl.py 的说明:
            # 这些值写 /dev/v4l-subdev2, 立即生效, 不需要重启 rkipc。
            self._json(200, cam_read())
        elif p == "/api/motordbg":
            # 电机路径的运行时内部状态。排查"按了没反应"用:
            #   calls       —— 指令有没有走到 motor.drive()
            #   dir_writes  —— 方向引脚到底写没写
            #   pwm_edges   —— PWM 有没有真的在翻转 (有调制在跑的铁证)
            #   pwm_alive   —— PWM 线程还活着吗
            #   pwm_err/err —— PWM 循环有没有被异常打死 (以前会静默退出)
            md = None
            try:
                if motor is not None and hasattr(motor, "dbg"):
                    md = dict(motor.dbg)
                    md["uptime_s"] = round(time.time() - md.get("t0", time.time()), 1)
                    md.pop("t0", None)
            except Exception as e:
                md = {"err": str(e)}
            with MOTOR_LOCK:
                snap = {k: STATE.get(k) for k in ("dir", "vx", "vy", "w", "speed", "ts")}
            snap["cmd_age"] = round(time.time() - snap["ts"], 2)
            self._json(200, {"motor_ok": motor is not None, "dbg": md, "state": snap})
        elif p == "/api/failsafe":
            # 看 + 调失控保护。
            #   GET /api/failsafe              -> 当前超时 + 误停统计
            #   GET /api/failsafe?t=0.5        -> 热切换超时 (立刻生效, 不用重启)
            # over_pct 就是"间隔超过超时的比例", 也就是**误停率** ——
            # 它一直高于 5% 就说明超时调得太短了, 别靠感觉判断。
            q = parse_qs(urlparse(self.path).query)
            if "t" in q:
                ok, val = set_failsafe_timeout(q["t"][0])
                self._json(200, {"ok": ok, "now": val, "stats": _fs_stats()})
            else:
                self._json(200, _fs_stats())
        elif p == "/api/video":
            # 视频档位: 分辨率 + 码率的预设组合。见 video_ctl.py。
            self._json(200, video_info())
        elif p == "/api/exposure":
            # 曝光档位。见 video_ctl.py 的 EXPOSURE 说明 ——
            # raw V4L2 exposure 在这块板子上调不动 (AE 在 rkaiq 里, V4L2
            # 层关不掉), 真正有效的是 rkipc 的 exposure_time 档位。
            self._json(200, exposure_info())
        elif p == "/api/quality":
            # 画面质量: 亮度/对比度/饱和度/锐度 + 增益上限 + WDR。
            # 治过曝的关键是"增益"和"WDR"两项 —— 实测选 1/1000 快档时
            # 曝光被钉死但增益还是 auto, ISP 为了补亮把增益推到 7000+,
            # 亮部直接溢出。见 video_ctl.py 的 QUALITY 说明。
            self._json(200, quality_info())
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # ---- POST ----
    def do_POST(self):
        p = urlparse(self.path).path
        d = self._body()

        # ---- 指令追踪 (诊断): 把每条控制指令连"谁发的"一起记下来 ----
        # 2026-10-05 加: 控制"时好时坏"时, 必须能回答"这段时间到底有没有
        # 指令到达、从哪个客户端、发的什么"。只写一行, 单核板上开销可忽略。
        # 关掉只需 rm /userdata/cmd_trace_on
        if p in ("/api/move", "/api/cmd", "/api/speed"):
            _fs_note_cmd()          # 统计指令到达间隔 (用于判断失控超时是否过短)

            try:
                if os.path.exists("/userdata/cmd_trace_on"):
                    with open("/userdata/cmd_trace.log", "a", encoding="utf-8") as f:
                        f.write("%s %s:%s %s %s\n" % (
                            time.strftime("%H:%M:%S"),
                            self.client_address[0], self.client_address[1],
                            p, json.dumps(d, ensure_ascii=False)))
            except Exception:
                pass

        # 摄像头调节不依赖电机, 所以放在电机检查之前
        if p == "/api/cam":
            try:
                ok, msg = cam_set(
                    exposure=d.get("exposure"),
                    gain=d.get("gain"),
                    lock=d.get("lock"),
                )
                self._json(200, {"ok": ok, "msg": msg, "cam": cam_read()})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return

        # ---- 页面自报家门: 记录到底跑的是哪一版前端 ----
        # 排"按了没反应"时, 先要能回答"浏览器里是哪一版页面"。
        if p == "/api/pageinfo":
            try:
                with open("/userdata/pageinfo.log", "a", encoding="utf-8") as f:
                    f.write("%s %s:%s %s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.client_address[0], self.client_address[1],
                        json.dumps(d, ensure_ascii=False)))
            except Exception:
                pass
            self._json(200, {"ok": True, "page_ver": _page_ver()})
            return

        if p == "/api/video":
            # 切换视频档位。**同步执行, 约 20-30 秒** —— 因为要重启 rkipc,
            # 而重启 rkipc 有已知的 554 socket 残留问题, 必须等它验证完
            # 才能告诉用户成功还是失败。前端要显示"切换中"。
            try:
                key = d.get("profile")
                if not key:
                    self._json(400, {"ok": False, "err": "缺少 profile 参数"})
                    return
                ok, msg = video_switch(key)
                self._json(200, {"ok": ok, "msg": msg,
                                 "video": video_info()})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return

        if p == "/api/exposure":
            # 切换曝光档位。**同步执行, 约 20-30 秒** —— 和画质档位一样
            # 要重启 rkipc 才生效。前端要显示"切换中"。
            try:
                key = d.get("profile")
                if not key:
                    self._json(400, {"ok": False, "err": "缺少 profile 参数"})
                    return
                ok, msg = exposure_switch(key)
                self._json(200, {"ok": ok, "msg": msg,
                                 "exposure": exposure_info()})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return

        if p == "/api/quality":
            # 改画面参数 (亮度/对比度/饱和度/锐度/增益/WDR)。
            # **同步执行, 约 20-30 秒** —— 要重启 rkipc 才生效。
            # 请求体只传要改的字段, 例如:
            #   {"gain":"low"}                 仅压低增益上限 (治过曝)
            #   {"wdr":"mid"}                  仅开 WDR
            #   {"brightness":60,"contrast":55}  同时调亮度和对比度
            try:
                ok, msg = quality_set(
                    brightness=d.get("brightness"),
                    contrast=d.get("contrast"),
                    saturation=d.get("saturation"),
                    sharpness=d.get("sharpness"),
                    gain=d.get("gain"),
                    wdr=d.get("wdr"),
                    over_exposure_suppress=d.get("over_exposure_suppress"),
                )
                self._json(200, {"ok": ok, "msg": msg, "quality": quality_info()})
            except Exception as e:
                self._json(500, {"ok": False, "err": str(e)})
            return

        if motor is None:
            self._json(503, {"ok": False, "err": "电机未初始化"})
            return

        if p == "/api/move":
            try:
                vx = float(d.get("vx", 0)); vy = float(d.get("vy", 0)); w = float(d.get("w", 0))
                s = int(d.get("s", STATE["speed"]))
            except Exception:
                self._json(400, {"ok": False, "err": "参数错误"}); return
            vx = max(-1.0, min(1.0, vx)); vy = max(-1.0, min(1.0, vy)); w = max(-1.0, min(1.0, w))
            s = max(0, min(100, s))
            with MOTOR_LOCK:
                if abs(vx) < 0.02 and abs(vy) < 0.02 and abs(w) < 0.02:
                    motor.stop(); STATE["dir"] = "stop"
                else:
                    motor.drive(vx, vy, w, s); STATE["dir"] = "move"
                STATE.update(vx=vx, vy=vy, w=w, speed=s, ts=time.time())
            self._json(200, {"ok": True})

        elif p == "/api/cmd":
            c = str(d.get("c", "stop")).lower()
            s = int(d.get("s", STATE["speed"]))
            s = max(0, min(100, s))
            with MOTOR_LOCK:
                if c == "forward":       motor.forward(s)
                elif c == "backward":    motor.backward(s)
                elif c == "spin_left":   motor.spin_left(s)
                elif c == "spin_right":  motor.spin_right(s)
                elif c == "strafe_left": motor.strafe_left(s)
                elif c == "strafe_right":motor.strafe_right(s)
                elif c == "brake":       motor.brake()
                else:                    motor.stop(); c = "stop"
                STATE.update(dir=c, speed=s, vx=0.0, vy=0.0, w=0.0, ts=time.time())
            self._json(200, {"ok": True, "dir": c})

        elif p == "/api/speed":
            s = max(0, min(100, int(d.get("s", 60))))
            with MOTOR_LOCK:
                STATE["speed"] = s
            self._json(200, {"ok": True, "speed": s})

        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

BOOT_MONO = time.monotonic()

def main():
    print("=" * 52)
    print(" Luckfox 遥控车 — 板载 Web 服务")
    print("=" * 52)
    # 失控超时: 先读配置, 再启动保护循环 (顺序不能反)
    init_motor()
    print("[failsafe] 失控超时 = %.2fs (来自 %s 的 failsafe_s, 缺省 %.1fs)"
          % (_load_failsafe_timeout(), CONF_PATH, MOVETIMEOUT_DEFAULT))

    threading.Thread(target=gps_reader, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=failsafe_loop, daemon=True).start()
    threading.Thread(target=cam_loop, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    except OSError as e:
        print("[http] 端口 %d 绑定失败: %s" % (PORT, e))
        sys.exit(1)
    print("[http] 监听 0.0.0.0:%d" % PORT)
    print("[http] 手机/电脑打开: http://<板子IP>:%d/" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if motor:
            motor.stop(); motor.close()

if __name__ == "__main__":
    main()

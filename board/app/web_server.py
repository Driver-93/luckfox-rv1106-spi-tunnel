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
import http.client          # 只用来调 mediamtx 的本地 API (见 mtx_enforce)
import sys_stats
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
        # 轴向取反 (见 car_motor.drive 的说明) —— 从 car_config.json 的
        # "axis_inv" 读, 例如 {"vx": true} 表示横移左右对调。
        # 用配置而不是改代码, 是因为"板子怎么装"属于装配差异,
        # 换一套安装方式不该改源码。
        inv = CONF.get("axis_inv") or {}
        # pwm_chip 必须从配置传入: car_motor 的 DEFAULT_PWM_CHIP 是旧接线的
        # 硬编码 (FL10/FR6/BL4/BR0), 2026-10-08 重接线后 (FR=11 BL=8) 不传
        # 就会按旧表 export, 表现为"pwmchip 不存在"。
        motor = FourMotor(CONF["pin"], simulate=False, inv=inv,
                          pwm_chip=CONF.get("pwm_chip"))
        motor.start_pwm()
        print("[motor] 已启动 (真实电机模式)  axis_inv=%s"
              % (inv if inv else "{}"))
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

def _gps_dev():
    """GPS 串口设备。car_config.json 里 "gps_uart" 可配 (默认 ttyS4)。

    2026-10-08: GPS 实际接在 UART1 (GPIO 68/69, 物理脚 21/22),
    板上配置为 /dev/ttyS1。uart4 (ttyS4) 是 9 月版方案, 引脚已被占用。
    """
    try:
        with open("/userdata/car/car_config.json", encoding="utf-8") as f:
            d = json.load(f)
        v = d.get("gps_uart")
        if v:
            return v
    except Exception:
        pass
    return "/dev/ttyS4"


_gps = {"present": os.path.exists(_gps_dev()), "fix": False,
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
    dev = _gps_dev()
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
        sysinfo = _tel.get("sys")
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

        # 页面版本 = index.html 的 mtime。        #
        # ⚠️ 2026-10-05 加, 因为踩了一次真实的坑: 用户浏览器里那个页面是
        # **上午 10:43 打开的**, 而我在 18:42 才改好前端的控制循环。旧页面
        # 一直跑着旧 JS ("只在数值变化时才发指令"), 于是板子的 1 秒失控保护
        # 不停被触发 —— 用户看到的是"车走走停停/控制不灵", 而我从板子侧
        # 怎么查都是好的 (链路健康、C5 没重启)。
        #
        # 有了这个字段, 页面每 1 秒轮询时就能发现"文件已更新", 自己 reload,
        # 不用再指望用户手动刷新 —— 这类"改了没生效"的故障太容易误判。
        "ver": _page_ver(),
        # 单客户端接管: 被 403 挡掉的请求统计。
        # 存在的意义就是让"控制被静默挡掉"这件事可见 —— 见 _DENIED 的说明。
        "denied": denied_view(),
        # 系统状态: CPU / 内存 / NPU / 检测 / 温度 / 磁盘。
        # 由 telemetry_loop 1Hz 刷新, 这里是缓存快照 (不额外开销)。
        "sys": sysinfo,
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
    "sys": None,
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
        # 系统状态 (CPU/内存/NPU/温度/磁盘) —— 只读 /proc 和 /sys 的小文件,
        # 实测很便宜; 但**仍然放在这个 1Hz 循环里**, 不单独提高频率。
        # 理由见本函数开头的警告: 把重采集提到高频会把单核打满。
        try:
            ss = sys_stats.sample()
        except Exception:
            ss = None
        with TEL_LOCK:
            _tel["mv"] = mv
            _tel["gps"] = dict(_gps)
            _tel["net4g"] = g4
            _tel["net"] = nw
            _tel["c3"] = c3
            _tel["sys"] = ss
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

# 指令追踪开关的缓存: 2 秒才 stat 一次, 避免每条指令都做系统调用。
_TRACE = {"on": False, "next": 0.0}


def _trace_tick():
    """指令追踪开关 (节流版)。返回 True 表示这次要写日志。

    原来在 do_POST 热路径上直接 os.path.exists(), 40Hz 下就是每秒
    40 次 stat; 而追踪默认关闭, 这些系统调用纯粹是浪费。
    """
    now = time.time()
    if now >= _TRACE["next"]:
        _TRACE["next"] = now + 2.0
        try:
            _TRACE["on"] = os.path.exists("/userdata/cmd_trace_on")
        except Exception:
            _TRACE["on"] = False
    return _TRACE["on"]


def _fs_note_cmd():
    """每条控制指令到达时调用一次 (do_POST 里)。

    ⚠️ 这里**不要**再抓 MOTOR_LOCK: 调用点在 /api/move 的 `with MOTOR_LOCK`
    之前一行, 等于每条指令(40Hz)多一次无谓的加解锁, 在单核板上会和
    HTTP/SPI 线程抢 GIL。只读 STATE 的几个标量, CPython 下是原子的。
    """
    try:
        now = time.time()
        prev = _fs["last_cmd"]
        _fs["last_cmd"] = now
        if prev <= 0:
            return
        gap = now - prev
        # 只在"车在动"时才统计: 停着的时候本来就不发心跳, 间隔大是正常的
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
# 快路径用的最小 headers 对象。stdlib 的 http.client.HTTPMessage 是大块头,
# 而控制接口只用得上 Content-Length。
# ============================================================================
# 单客户端接管 (2026-10-08): 一个时刻只允许一个页面上车
# ============================================================================
# 需求: 新打开的浏览器把旧的踢掉 —— 车只有一个司机, 而且多一个观众就多一份
# 隧道带宽 (总共 ~970KB/s) 和单核 CPU (mediamtx 要给**每个**观众单独做
# SRTP 加密 + 发送, rkipc 只编码一次是共享的)。
#
# ⚠️ 为什么不能只做"谁新谁赢, 直接踢掉旧的"
# ------------------------------------------
# index.html 的图传重连是**无限**的 (schedule(): 连败退避到 5s, 然后一直
# 重试下去)。只按下发时间踢, 会变成抢来抢去:
#
#     A 被踢 -> 5s 后自动重连, 变成"最新的" -> B 被踢 -> 2s 后 B 又抢回来 …
#
# 两个浏览器永远在互踢, 谁都看不成。所以**必须让被踢的那一方知道自己被踢了,
# 主动闭嘴**。现成的两个通道正好够用:
#
#   * /api/pageinfo  每次页面加载只发一次  => 天然的"新浏览器到了"锚点
#   * /api/status    每秒轮询一次          => 天然的"通知旧页面你被踢了"通道
#
# 反过来,**自动重连永远不认领所有权**, 所以它抢不回来。这是整个设计的关键:
# 认领只发生在页面加载 (pageinfo) 和用户点"接管"按钮 (takeover) 这两处,
# 都是"人主动做的动作"。
#
# 粒度是**页面标签**, 不是 IP
# ---------------------------
# 每个页面加载时生成一个随机 TAB_ID, 随请求头 X-Page-Id 上报。用 IP 做不到
# "同一台电脑开两个标签也互踢" —— 两个标签是同一个地址。用 TAB_ID 就干净了。
#
# 三层保险, 任何一层失效都不影响其它层
# ------------------------------------
#   1. 板子侧: 非所有者的控制请求一律 403; 认领的瞬间**主动停车一次**
#      (旧页面已经被 403 禁掉了, 指望它自己发 stop 不可靠)
#   2. 页面侧: 轮询发现 owner=false -> 停图传 + 停控制 + 显示横幅 (1 秒内)
#   3. mediamtx 兜底: 后台线程轮询 /v3/webrtcsessions/list, 把不属于当前
#      所有者的会话踢掉 —— 专门防"旧页面卡死了, 自己不会停"
#
# 第 3 层要 mediamtx 的 API (见 mediamtx.yml 的 api / apiAddress)。
# 失败方向是安全的: 任何一层出问题最多只是"没踢掉", 绝不会"连不上" ——
# API 拿不到时 mtx_enforce() 直接返回, 图传照常。
OWN_LOCK = threading.Lock()
_OWN = {"page": None, "ip": None, "since": 0.0, "n": 0}
MTX_API = ("127.0.0.1", 9997)
PAGE_HDR = "X-Page-Id"

# 需要"当前所有者"身份的 POST 接口。
#
# 包含摄像头/画质/曝光那三个: 它们会**重启 rkipc (20-30 秒)**, 被接管的旧
# 页面要是还能触发, 就成了"谁都能把图传掐掉"。
# 不含 /api/status (通知通道) 和 /api/ping (诊断探针)。
OWNED_POST_PATHS = frozenset((
    "/api/move", "/api/cmd", "/api/speed",
    "/api/cam", "/api/video", "/api/exposure", "/api/quality",
))

# mediamtx 会话清理状态。不比较时间戳, 见 mtx_enforce() 里的说明。
_MTX = {"gen": -1, "condemned": set()}

# 所有者存盘。
#
# 为什么必须存: 所有者本来只在内存里, 于是**每次重启这个服务, 所有者就没了**,
# 而 owner_claim 只发生在"页面加载"和"点接管按钮"这两个时刻 —— 已经打开着的
# 标签页不会再发 pageinfo。结果: 重启之后两个标签页都会看到 owner=true
# ("还没有人认领"), 双双在图传, 单客户端策略静默失效, 直到有人手动刷新。
#
# 存 /tmp (tmpfs): 重启服务时保留, 重启板子时自动清掉 —— 正是想要的语义。
# pageId 是页面加载时随机生成的, 所以恢复出来的那个 id 只会匹配"同一个标签页",
# 不会误伤别人。
OWN_FILE = "/tmp/owner.json"


def _owner_load():
    try:
        with open(OWN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("page"), str) and d["page"]:
            with OWN_LOCK:
                _OWN.update(page=d["page"], ip=d.get("ip") or None,
                            since=float(d.get("since") or 0.0),
                            n=int(d.get("n") or 0))
            print("[owner] 恢复所有者: page=%s ip=%s (%s)"
                  % (d["page"], d.get("ip"), OWN_FILE))
    except Exception:
        pass        # 文件不存在/坏了都无所谓, 下一个人加载页面就重新认领


def _owner_save():
    try:
        with OWN_LOCK:
            d = dict(_OWN)
        with open(OWN_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


# 被 403 挡掉的请求计数。
#
# 存在的唯一目的: 让"控制被静默挡掉"这件事**看得见**。
# 2026-10-08 踩的坑正是它反面 —— 前端漏发 X-Page-Id, 所有控制指令都被 403,
# 而两边都没有任何错误迹象 (前端把 403 当成功, 板子不记被拒的请求),
# 排查只能靠猜。现在 /api/status 里直接能看到 403 次数和最后一次是谁。
_DENIED = {"n": 0, "path": "", "page": "", "ip": "", "ts": 0.0}


def _note_denied(path, page, ip):
    try:
        _DENIED["n"] += 1
        _DENIED["path"] = path
        _DENIED["page"] = page or "(无页面标识)"
        _DENIED["ip"] = ip
        _DENIED["ts"] = time.time()
    except Exception:
        pass


def denied_view():
    with OWN_LOCK:
        pass
    d = dict(_DENIED)
    if d["ts"]:
        d["ago"] = round(time.time() - d["ts"], 1)
    d.pop("ts", None)
    return d


def owner_view(page):
    """给 /api/status 用的所有权快照。"""
    with OWN_LOCK:
        cur = _OWN["page"]
        n = int(_OWN.get("n", 0))
    if not page:
        # 没带 TAB_ID 的客户端 (旧页面 / 探针)。控制类接口会被 403 挡掉,
        # 只读接口照常 —— 别把诊断用的 /api/ping 也一起挡了。
        return {"owner": False, "no_id": True, "takeovers": n}
    if cur is None:
        # 还没有人认领 (板子刚起来, 或者页面刚加载、pageinfo 还没到)。
        # 这里**只读不认领** —— 认领必须发生在页面加载那一刻, 否则被踢的
        # 页面靠每秒轮询就能把自己抢回来, 那就又变成抢来抢去了。
        return {"owner": True, "pending": True, "takeovers": n}
    return {"owner": cur == page, "takeovers": n}


def _snapshot_live_sessions():
    """把此刻还活着的 mediamtx 会话全部记为待踢。

    必须在**认领的同一刻**调用, 不能等 enforce_loop 下一秒醒来再做。
    原因: "凡是活着的会话都属于上一个页面" 这句话只在认领那一刻成立 ——
    新页面紧接着就要开始拉流 (index.html 的 claimThenStart() 保证先认领、
    后拉流)。晚一秒再快照, 就会把新页面自己刚建立的会话也判死, 白踢一次,
    用户看到多一次 2 秒的图传重连。
    """
    try:
        st, body = _mtx_api("GET", "/v3/webrtcsessions/list")
        if st != 200:
            return
        data = json.loads(body.decode("utf-8")) or []
        items = data.get("items") if isinstance(data, dict) else data
        ids = set(it["id"] for it in (items or [])
                  if isinstance(it, dict) and it.get("id"))
    except Exception:
        return          # mediamtx 没起来/没开 API: 跳过, 兜底层让位于前两层
    with OWN_LOCK:
        _MTX["condemned"] = ids
        _MTX["gen"] = int(_OWN.get("n", 0))


def owner_claim(page, ip):
    """认领所有权。返回 (ok, 是否发生了接管)。"""
    if not page:
        return False, False
    with OWN_LOCK:
        prev_page, prev_ip = _OWN["page"], _OWN["ip"]
        changed = (prev_page != page)
        _OWN.update(page=page, ip=ip, since=time.time())
        if changed:
            _OWN["n"] = int(_OWN.get("n", 0)) + 1
    if changed:
        _owner_save()
        print("[owner] 接管: page=%s ip=%s (上一个 page=%s ip=%s)"
              % (page, ip, prev_page, prev_ip))
        try:
            with open("/userdata/takeover.log", "a", encoding="utf-8") as f:
                f.write("%s 接管 page=%s ip=%s (上一个 page=%s ip=%s)\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           page, ip, prev_page, prev_ip))
        except Exception:
            pass
        # 快照要在 stop 之前 —— 两件事互不影响, 但先把"谁该被踢"记下来,
        # 后面即使 stop 出问题也不影响这条链路。
        _snapshot_live_sessions()
        if prev_page is not None:
            _stop_now("被 page=%s 接管" % page)
    return True, changed


def _stop_now(reason):
    """立刻停车。

    接管时必须主动停一次, 不能只依赖 0.5s 失控保护: 旧页面此刻已经被 403
    挡掉了, "让它自己发一条 stop" 是不可靠的。安全关键路径, 所以整个函数
    包在 try 里 —— 它绝不能让 HTTP 处理器挂掉。
    """
    try:
        now = time.time()
        with MOTOR_LOCK:
            if motor is not None:
                motor.stop()
            STATE.update(dir="stop", vx=0.0, vy=0.0, w=0.0, ts=now)
        try:
            with open("/userdata/takeover.log", "a", encoding="utf-8") as f:
                f.write("%s 接管停车: %s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), reason))
        except Exception:
            pass
    except Exception:
        pass


def _mtx_api(method, path):
    """调 mediamtx 的本地 API。返回 (status, body); 失败返回 (0, b"")。"""
    try:
        c = http.client.HTTPConnection(MTX_API[0], MTX_API[1], timeout=2)
        c.request(method, path)
        r = c.getresponse()
        b = r.read()
        c.close()
        return r.status, b
    except Exception:
        return 0, b""


def mtx_enforce():
    """把不属于当前所有者的 WebRTC 会话踢掉 (兜底层)。

    "不属于" 的判据只有两条, 而且**不需要时间戳**:
      * 对端 IP 不是所有者的 -> 别的设备, 踢
      * 对端 IP 是所有者, 但这个会话在**最近一次接管之前就存在** -> 旧的
        标签页留下的, 踢

    为什么不用 mediamtx 的 created 字段比时间: 它是带时区的 RFC3339, 在这块
    uClibc 板子上解析 + 时区对齐都是额外的出错面; 而"接管瞬间还活着的会话都
    该死"这句话不需要时间戳就能表达 —— 记下那一刻的 id 集合, 之后只要它们还
    出现就继续踢 (消失了就忘掉)。
    """
    with OWN_LOCK:
        page, ip = _OWN["page"], _OWN["ip"]
        gen = int(_OWN.get("n", 0))
    if not page or not ip:
        return

    st, body = _mtx_api("GET", "/v3/webrtcsessions/list")
    if st != 200:
        return
    try:
        data = json.loads(body.decode("utf-8")) or []
    except Exception:
        return
    # ⚠️ mediamtx v1.11.3 返回的是**分页信封**, 不是一个裸数组:
    #     {"itemCount":0,"pageCount":0,"items":[]}
    # 老版本返回裸数组。只认数组的话这里会永远静默返回 —— 表现为"接管了但
    # 旧的图传没断", 而且日志里一行都不出, 极难排查 (实测踩到)。两种都收。
    if isinstance(data, dict):
        items = data.get("items") or []
    else:
        items = data
    if not isinstance(items, list):
        return

    live = {}
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            # remoteAddr 形如 "192.168.3.65:57476"; 取 IP 部分。
            live[it["id"]] = str(it.get("remoteAddr", ""))

    with OWN_LOCK:
        if gen != _MTX["gen"]:
            # 兜底: 所有权变了但没走 owner_claim 的快照 (例如 enforce_loop
            # 在 owner_claim 的 API 调用失败之后才醒来)。此时只能拿眼前这份
            # 列表当快照 —— 不如认领那一刻准, 但比不踢安全。
            _MTX["condemned"] = set(live.keys())
            _MTX["gen"] = gen
        condemn = set(_MTX["condemned"])
        _MTX["condemned"] = {s for s in condemn if s in live}   # 消失的忘掉

    for sid, addr in live.items():
        peer = addr.rsplit(":", 1)[0] if addr else ""
        if peer == ip and sid not in condemn:
            continue                       # 当前页面自己的会话, 留着
        st2, _ = _mtx_api("POST", "/v3/webrtcsessions/kick/" + sid)
        if st2 == 200:
            print("[owner] 踢掉旧图传会话 %s (对端 %s)" % (sid, addr))


def enforce_loop():
    while True:
        try:
            mtx_enforce()
        except Exception:
            pass
        time.sleep(1.0)


class _Headers:
    """快路径用的极简 headers 对象。

    只保留服务端真正要看的头 —— 构造 http.client.HTTPMessage 去解析全部头
    在单核 A7 上不划算 (见 handle_one_request 的说明)。

    ⚠️ 加新头时**必须**同时改 handle_one_request 里的扫描循环, 否则这里的
    get() 永远返回 default。单客户端接管第一次就是栽在这: X-Page-Id 没被
    扫进来, 于是每个请求都被当成"没有页面标识", **所有**控制请求一律 403
    (连当前所有者自己的也被挡掉), 而日志里一行错都没有 —— 只表现为
    "按了没反应"。所以扫描循环和这里的键名必须成对维护。
    """
    __slots__ = ("_cl", "_page")

    def __init__(self, content_length=0, page=""):
        self._cl = int(content_length or 0)
        self._page = page or ""

    def get(self, name, default=None):
        n = (name or "").lower()
        if n == "content-length":
            return str(self._cl)
        if n == "x-page-id":
            return self._page
        return default

    def get_all(self, name, default=None):
        v = self.get(name)
        return [v] if v is not None else (default or [])

    def __getitem__(self, name):
        v = self.get(name)
        if v is None:
            raise KeyError(name)
        return v

    def __contains__(self, name):
        return self.get(name) is not None


_REASON = {
    200: b"OK", 204: b"No Content", 301: b"Moved Permanently",
    302: b"Found", 304: b"Not Modified", 400: b"Bad Request",
    403: b"Forbidden", 404: b"Not Found", 405: b"Method Not Allowed",
    408: b"Request Timeout", 413: b"Payload Too Large",
    414: b"URI Too Long", 500: b"Internal Server Error",
    503: b"Service Unavailable",
}


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

    # ---- 控制热路径加速 (2026-10-06) ----
    # 用户报"按住方向键 CPU 到 80-90%"。实测定位 (板子自己测自己):
    #
    #   PWM 线程       空闲 9.0%  →  按住时 8.9%   ← 完全没变, 不是它
    #   HTTP 处理线程  空闲 0.5%  →  按住时 13.1%  ← 就是它
    #
    # 进一步用"极小 handler"对照 (同样走 BaseHTTPRequestHandler, 但 do_POST
    # 里只读 body 就回 11 字节):
    #
    #   BaseHTTPRequestHandler 极简版   4.19 ms/请求
    #   裸 socket 极简版                1.16 ms/请求   ← 快 3.6 倍
    #
    # 也就是说 **4.19ms 里绝大部分是 stdlib 自己的开销**, 不是我们的代码:
    # parse_request() 是纯 Python 逐行扫 header, 每条请求还要建 Date 头、
    # 走 send_response/send_header 的缓冲逻辑。在单核 A7 上 40Hz 就是
    # 40 * 4.19ms = 17% 的核, 加上真正的业务和视频编码就顶到 80%+。
    #
    # 修法: 给**控制类请求**加一条手写快路径 —— 只解析我们真正用到的
    # 三个头 (Content-Length / Connection / 请求行), 直接把响应拼成一次
    # sendall 发出去 (顺带省掉第二次 write, Nagle 也就不用管了)。
    # 页面本身和冷门接口仍然走标准 stdlib 路径, 保证兼容性。
    _FAST_PATHS = ("/api/move", "/api/cmd", "/api/speed",
                   "/api/ping", "/api/status")

    def handle_one_request(self):
        """替代 stdlib 的实现: 对控制类请求走快路径, 其余交回 stdlib。

        ⚠️ 关键点: 请求行已经从这里读走了, 所以**不能**再去调
        BaseHTTPRequestHandler.handle_one_request —— 它会从 rfile 再读一行,
        而那已经是下一个请求 (或 EOF), 结果是响应错位、客户端报
        "服务器提交了协议冲突"。非热路径必须在这里自己把状态补全, 然后
        直接进 do_GET/do_POST。
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
        except Exception:
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return

        line = self.raw_requestline
        sp1 = line.find(b" ")
        sp2 = line.find(b" ", sp1 + 1)
        if sp1 < 0 or sp2 < 0:
            # 不像 HTTP 请求行: 直接关掉, 不要试图"交回 stdlib"
            self.close_connection = True
            return

        command = line[:sp1]
        # --- header 扫描 (快慢路径共用, 一次读完) ---
        # 只留我们可能用到的头, 避免构造 http.client.HTTPMessage (大块头)。
        #
        # ⚠️ 这里扫进来的每一个头, 都必须在 _Headers 里有对应的 get() 分支,
        # 否则读到的永远是 default (单客户端接管第一次就是栽在这里:
        # x-page-id 漏扫 -> 所有控制请求被误判成"无页面标识" -> 一律 403)。
        clen = 0
        page = ""
        while True:
            try:
                h = self.rfile.readline(65537)
            except Exception:
                self.close_connection = True
                return
            if not h or h in (b"\r\n", b"\n"):
                break
            if h[0] in (0x20, 0x09):        # 折行, 忽略
                continue
            c = h.find(b":")
            if c < 0:
                continue
            k = h[:c].strip().lower()
            if k == b"content-length":
                try:
                    clen = int(h[c + 1:].strip())
                except ValueError:
                    clen = 0
            elif k == b"x-page-id":
                page = h[c + 1:].strip().decode("latin-1", "replace")[:64]

        body = b""
        if clen > 0:
            if clen > 262144:
                self.close_connection = True
                try:
                    self.send_error(413)
                except Exception:
                    pass
                return
            try:
                body = self.rfile.read(clen)
            except Exception:
                self.close_connection = True
                return

        self.command = command.decode("latin-1")
        self.request_version = "HTTP/1.1"
        self.path = line[sp1 + 1:sp2].decode("latin-1")
        self.requestline = "%s %s %s" % (self.command, self.path,
                                         self.request_version)
        self.close_connection = False
        self.headers = _Headers(clen, page)
        self._body_raw = body
        try:
            if self.command == "POST":
                self.do_POST()
            elif self.command == "GET":
                self.do_GET()
            elif self.command == "HEAD":
                self.do_GET()
            else:
                self.send_error(501)
            try:
                self.wfile.flush()
            except Exception:
                self.close_connection = True
        except Exception:
            # 处理器里出异常不能让连接线程带崩整个服务
            self.close_connection = True

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # 一次 write 拼完整包: 少一次 write 系统调用, 也不再依赖
        # TCP_NODELAY 去压第二次小包的延迟。
        try:
            self.wfile.write(
                b"HTTP/1.1 %d %s\r\n"
                b"Content-Type: %s\r\n"
                b"Content-Length: %d\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: keep-alive\r\n\r\n"
                % (code, _REASON.get(code, b"OK"), ctype.encode("latin-1"),
                   len(body)) + body)
        except Exception:
            self.close_connection = True

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        # 快路径已经把 body 读出来了, 直接用, 不再碰 rfile
        raw = getattr(self, "_body_raw", None)
        if raw is not None:
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _page_id(self):
        """这个请求来自哪个页面标签 (单客户端接管用)。见文件末尾的说明。

        没有就返回空串 —— 旧页面 (还没加载新 JS) 和 curl/探针都属于这一类。
        """
        try:
            return (self.headers.get(PAGE_HDR) or "").strip()[:64]
        except Exception:
            return ""

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
            st = build_status()
            # 所有权状态。页面靠它发现自己被接管了 (1 秒内)。
            # ⚠️ 这个接口**永远不能被 403 挡掉** —— 它正是"通知旧页面你被
            # 踢了"的唯一通道, 挡掉旧页面就永远不知道自己该闭嘴了。
            st["owner"] = owner_view(self._page_id())
            self._json(200, st)
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
                # 改失控超时是**安全参数**, 只有当前所有者能改 ——
                # 否则被接管的旧页面可以把超时拉长, 让车在没人控制时继续跑。
                if not owner_view(self._page_id()).get("owner"):
                    self._json(403, {"ok": False, "taken_over": True,
                                     "err": "已被其它设备接管"})
                    return
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
        page = self._page_id()

        # ---- 单客户端接管: 认领 / 抢回 (见文件末尾的长说明) ----
        # 只有这两个入口能认领所有权, 而且都是**人主动做的动作**:
        #   * /api/pageinfo  每次页面加载只发一次
        #   * /api/takeover  横幅上那个"点此接管回来"按钮
        # 自动重连永远走不到这里, 所以它抢不回所有权 —— 这是避免两个浏览器
        # 无限互踢的关键。
        if p == "/api/takeover":
            ok, _ = owner_claim(page, self.client_address[0])
            self._json(200 if ok else 400,
                       {"ok": ok, "err": None if ok else "缺少页面标识",
                        "owner": owner_view(page)})
            return

        # ---- 控制类接口: 只有当前所有者能动 ----
        # 被接管之后旧页面一律 403。返回体带 taken_over, 前端据此显示横幅。
        # 注意 /api/status 和 /api/ping **不**在这里 —— 前者是通知通道,
        # 后者是诊断探针, 挡掉它们只会让排查变难。
        #
        # ⚠️ "完全没有 page id" 的客户端**只在已经有人上车时才挡**。
        #
        # 这一条是踩坑之后加的: 最初写的是 `if not page or ...` —— 只要没带
        # 页面标识就一律 403。结果前端有一条控制路径 (ctlSend) 漏发了
        # X-Page-Id, 于是**每一条控制指令都被 403**, 车完全不动; 而前端当时
        # 又没检查状态码, 把它当成成功, 页面上一切正常 —— 零错误迹象。
        #
        # 现在: 没人上车时, 不带标识的客户端 (旧页面 / curl / 脚本) 照常能
        # 控制 —— 那是这个功能引入**之前**的行为, 不会因为新功能而变砖。
        # 一旦有人认领了所有权, 规则立刻恢复严格。
        if p in OWNED_POST_PATHS:
            with OWN_LOCK:
                cur = _OWN["page"]
            denied = (cur is not None and cur != page) if page else (cur is not None)
            if denied:
                _note_denied(p, page, self.client_address[0])
                self._json(403, {"ok": False, "taken_over": True,
                                 "err": "已被其它设备接管" if page else
                                        "需要页面标识 (请刷新页面)"})
                return

        # ---- 指令追踪 (诊断): 把每条控制指令连"谁发的"一起记下来 ----
        # 2026-10-05 加: 控制"时好时坏"时, 必须能回答"这段时间到底有没有
        # 指令到达、从哪个客户端、发的什么"。
        #
        # 2026-10-06 修: 这里原来**每条指令**都要 os.path.exists()
        # (40Hz 就是每秒 40 次 stat 系统调用), 而这是纯诊断功能, 默认就是
        # 关的。改成每 2 秒才查一次开关, 热路径上只剩一次时间比较。
        if p in ("/api/move", "/api/cmd", "/api/speed"):
            _fs_note_cmd()          # 统计指令到达间隔 (用于判断失控超时是否过短)

            if _trace_tick():
                try:
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

        # ---- 页面自报家门 + **认领所有权** ----
        # 每次页面加载只发一次, 所以这就是"新浏览器到了"的锚点。见文件末尾
        # "单客户端接管" 的说明: 认领只在这一处和 /api/takeover 发生。
        if p == "/api/pageinfo":
            try:
                with open("/userdata/pageinfo.log", "a", encoding="utf-8") as f:
                    f.write("%s %s:%s %s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.client_address[0], self.client_address[1],
                        json.dumps(d, ensure_ascii=False)))
            except Exception:
                pass
            ok, took = owner_claim(page, self.client_address[0])
            self._json(200, {"ok": True, "page_ver": _page_ver(),
                             "claim": ok, "takeover": took,
                             "owner": owner_view(page)})
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


# ---- 服务器选择 (2026-10-06) ----
# ThreadingHTTPServer 对**每条 TCP 连接**起一个线程。浏览器保活复用连接,
# 所以连接数不多; 但 stdlib 的 accept 循环 + 每条连接的线程创建在单核
# A7 上仍然很贵。实测对照:
#
#   极简 stdlib handler (什么都不做)  4.21 ms/请求
#   极简裸 socket handler             1.13 ms/请求
#
# 我们改造后的控制 handler 现在只要 ~3.3 ms/请求 —— 已经比"空的 stdlib
# handler"还快, 说明剩下的开销在服务器架构而不是业务代码。
#
# ⚠️ 为什么**不**改成固定 worker 池: HTTP/1.1 keep-alive 下, 一个连接
# 会占用一个处理器线程直到空闲超时 (15s)。浏览器保持 2-3 条长连接, 固定
# 池立刻就被占满, 新连接会饿死。所以这里保留 ThreadingHTTPServer 的
# "每连接一线程"模型 —— 它在这个场景下反而是对的: 线程数 = 活跃连接数,
# 而活跃连接数由浏览器决定 (通常 2-6 条, 不是 40 条)。
#
# 真正的开销来自**每次请求**的 stdlib 解析, 那部分已经在上面的
# handle_one_request 快路径里消掉了。
def main():
    print("=" * 52)
    print(" Luckfox 遥控车 — 板载 Web 服务")
    print("=" * 52)
    # 失控超时: 先读配置, 再启动保护循环 (顺序不能反)
    init_motor()
    # 恢复"谁在上车"。见 OWN_FILE 的说明 —— 不恢复的话, 重启服务之后
    # 两个标签页会同时以为自己可以控制 (单客户端策略静默失效)。
    _owner_load()
    print("[failsafe] 失控超时 = %.2fs (来自 %s 的 failsafe_s, 缺省 %.1fs)"
          % (_load_failsafe_timeout(), CONF_PATH, MOVETIMEOUT_DEFAULT))

    threading.Thread(target=gps_reader, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=failsafe_loop, daemon=True).start()
    threading.Thread(target=cam_loop, daemon=True).start()
    # 单客户端接管的兜底层: 每秒问一次 mediamtx, 把不属于当前所有者的
    # WebRTC 会话踢掉。见文件末尾 "单客户端接管" 的说明。
    threading.Thread(target=enforce_loop, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
        # 单核板上 GIL 是稀缺资源: 让 accept 循环少醒一点, 把 CPU 让给
        # 控制线程和视频编码。0.5s(默认) -> 0.2s 只是让统计线程更及时,
        # 但注意 poll_interval 只影响"没有连接进来"时的空转频率。
        srv.timeout = 0.2
    except OSError as e:
        print("[http] 端口 %d 绑定失败: %s" % (PORT, e))
        sys.exit(1)
    print("[http] 监听 0.0.0.0:%d" % PORT)
    print("[http] 手机/电脑打开: http://<板子IP>:%d/" % PORT)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if motor:
            motor.stop(); motor.close()

if __name__ == "__main__":
    main()

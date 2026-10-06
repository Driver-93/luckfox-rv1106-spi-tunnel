#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Luckfox 4G 遥控小车 - MQTT 控制守护程序 (4WD 麦克纳姆轮) + 遥测
订阅命令主题, 驱动四路 TB6612 电机, 上报状态与遥测 (电池电压 / GPS / 4G 模块)。

MQTT 协议:
  SUB  luckfox/car/cmd    {"c":"forward|backward|spin_left|spin_right|strafe_left|strafe_right|stop|brake","s":0-100,"token":..}
  PUB  luckfox/car/status {"mode":..,"dir":..,"speed":..,"online":true,
                           "tel":{"bat_mv":..,"bat_v":..,"gps":{...},"net4g":{...}}}

用法:
  python3 car_controller.py            # 用 /etc/luckfox-car/car_config.json
  python3 car_controller.py --simulate # 无硬件模拟
"""
import json, time, sys, os, glob, threading, argparse, uuid
import paho.mqtt.client as mqtt
from car_motor import FourMotor

CONF_PATH = "/etc/luckfox-car/car_config.json"
ADC_IIO_GLOB = "/sys/bus/iio/devices/iio:device*/in_voltage%d_raw"
ADC_SCALE_MV = 1.7578125  # RV1106 SARADC 满量程 1.8V / 1024
ADC_OVERSAMPLE = 16       # 过采样次数
ADC_TRIM = 16             # 截尾样本数
GPS_DEVICE = "/dev/ttyS4"  # uart4, 物理 6/7 脚
GPS_BAUD = 9600

def load_conf():
    conf = {
        "mqtt": {"broker": "broker.emqx.io", "port": 1883,
                 "topic_cmd": "luckfox/car/cmd", "topic_status": "luckfox/car/status",
                 "heartbeat": 5,
                 "username": None, "password": None,
                 "use_tls": False, "ca_cert": None, "token": None},
        "pin": {"STBY": -1,
                "FL": {"IN1":-1,"IN2":-1,"PWM":-1},
                "FR": {"IN1":-1,"IN2":-1,"PWM":-1},
                "BL": {"IN1":-1,"IN2":-1,"PWM":-1},
                "BR": {"IN1":-1,"IN2":-1,"PWM":-1}},
        "telemetry": {"adc_channel": 1,      # 物理 32 脚 = SARADC_IN1 (驱动板 ADC 输出)
                      "adc_ratio": 8.97,     # 驱动板分压比: VBAT = V_adc * ratio (过采样标定)
                      "gps_enabled": True,
                      "check_4g": True},
    }
    try:
        with open(CONF_PATH) as f:
            user = json.load(f)
        conf["mqtt"].update(user.get("mqtt", {}))
        p = conf["pin"]
        up = user.get("pin", {})
        p["STBY"] = up.get("STBY", p["STBY"])
        for ch in ("FL","FR","BL","BR"):
            for k in ("IN1","IN2","PWM"):
                p[ch][k] = up.get(ch, {}).get(k, p[ch][k])
        conf["telemetry"].update(user.get("telemetry", {}))
    except Exception as e:
        print("[conf] 配置缺失, 用默认(模拟):", e)
    return conf

# ---------------- 遥测: 电池电压 (SARADC) ----------------
def read_adc_mv(channel):
    """返回 ADC 通道毫伏值, 失败返回 None. 过采样+截尾均值抗抖."""
    for path in glob.glob(ADC_IIO_GLOB % channel):
        vals = []
        try:
            f = open(path)
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
        t = ADC_TRIM if len(vals) > 2 * ADC_TRIM + 1 else len(vals) // 2
        core = vals[t:len(vals) - t] or vals
        raw = sum(core) / float(len(core))
        if raw >= 1022:  # 悬空满量程, 视为未接
            return None
        return raw * ADC_SCALE_MV
    return None

# ---------------- 遥测: GPS (uart4 NMEA) ----------------
class GpsReader(threading.Thread):
    """后台读 NMEA, 维护最新定位; 无设备时静默"""
    def __init__(self, device=GPS_DEVICE, baud=GPS_BAUD):
        super().__init__(daemon=True)
        self.device = device
        self.baud = baud
        self.data = {"present": os.path.exists(device), "fix": False,
                     "lat": None, "lon": None, "sats": None,
                     "speed_kmh": None, "utc": None, "ts": None}

    @staticmethod
    def _nmea_coord(raw, hemi):
        if not raw or not hemi:
            return None
        try:
            # ddmm.mmmm -> 度
            v = float(raw)
            deg = int(v // 100)
            minutes = v - deg * 100
            val = deg + minutes / 60.0
            if hemi in ("S", "W"):
                val = -val
            return round(val, 6)
        except ValueError:
            return None

    def _parse(self, line):
        try:
            if line.startswith("$GPGGA") or line.startswith("$GNGGA"):
                f = line.split(",")
                if len(f) > 9:
                    self.data["sats"] = int(f[7]) if f[7] else None
                    fix = f[6]
                    self.data["fix"] = fix in ("1", "2", "4", "5")
                    self.data["lat"] = self._nmea_coord(f[2], f[3] if len(f) > 3 else None)
                    self.data["lon"] = self._nmea_coord(f[4], f[5] if len(f) > 5 else None)
                    if f[1]:
                        self.data["utc"] = f[1]
                    self.data["ts"] = int(time.time())
            elif line.startswith("$GPRMC") or line.startswith("$GNRMC"):
                f = line.split(",")
                if len(f) > 8:
                    if f[2] == "A":
                        self.data["fix"] = True
                        self.data["lat"] = self._nmea_coord(f[3], f[4] if len(f) > 4 else None)
                        self.data["lon"] = self._nmea_coord(f[5], f[6] if len(f) > 6 else None)
                        self.data["ts"] = int(time.time())
                        if len(f) > 7 and f[7]:
                            # 节 -> km/h
                            try:
                                self.data["speed_kmh"] = round(float(f[7]) * 1.852, 1)
                            except ValueError:
                                pass
        except Exception:
            pass

    def run(self):
        if not self.data["present"]:
            return
        fd = None
        try:
            import termios
            fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, "B9600", termios.B9600)
            attrs[0] = attrs[1] = 0          # raw
            attrs[2] = speed | termios.CS8 | termios.CLOCAL | termios.CREAD
            attrs[3] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            buf = b""
            while True:
                try:
                    chunk = os.read(fd, 512)
                except BlockingIOError:
                    time.sleep(0.2)
                    continue
                except OSError:
                    break
                if chunk:
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            self._parse(line.decode("ascii", "ignore").strip())
                        except Exception:
                            pass
                else:
                    time.sleep(0.5)
        except Exception as e:
            print(f"[gps] {e}")
        finally:
            if fd is not None:
                try: os.close(fd)
                except Exception: pass
            self.data["present"] = False

# ---------------- 遥测: 4G 模块 ----------------
def check_4g():
    """检查 EC801E 是否在 USB 总线上"""
    try:
        for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            with open(d) as f:
                if f.read().strip().lower() == "2c7c":
                    base = os.path.dirname(d)
                    prod = ""
                    try:
                        with open(os.path.join(base, "product")) as f2:
                            prod = f2.read().strip()
                    except Exception:
                        pass
                    return {"present": True, "module": prod}
    except Exception:
        pass
    return {"present": False, "module": None}

class Car:
    def __init__(self, conf, simulate):
        self.conf = conf
        self.dir = "stop"
        self.speed = 0
        self.motor = FourMotor(conf["pin"], simulate=simulate)
        if not simulate:
            self.motor.start_pwm()
        self.sim = simulate
        tel = conf.get("telemetry", {})
        self.adc_channel = tel.get("adc_channel", 1)
        self.adc_ratio = float(tel.get("adc_ratio", 11.0))
        self.gps = GpsReader() if tel.get("gps_enabled", True) else None
        if self.gps:
            self.gps.start()
        self.check_4g = tel.get("check_4g", True)

    def handle(self, payload):
        try:
            data = json.loads(payload)
        except Exception:
            data = {"c": payload.decode().strip().lower(), "s": 50}
        # token 鉴权: 若配置了 token, 指令必须带正确的 token 才执行
        expect = self.conf["mqtt"].get("token")
        if expect:
            got = str(data.get("token", "") if isinstance(data, dict) else "")
            if got != expect:
                print(f"[sec] 拒绝指令: token 不匹配")
                return
        c = str(data.get("c", "stop")).lower()
        s = int(data.get("s", 50))
        s = max(0, min(100, s))
        self.speed = s
        alias = {"f":"forward","b":"backward","back":"backward","front":"forward",
                 "turn_left":"spin_left","turn_right":"spin_right",
                 "strafeL":"strafe_left","strafeR":"strafe_right"}
        c = alias.get(c, c)
        if c == "move":
            # 连续向量控制 (摇杆): {"c":"move","vx":..,"vy":..,"w":..,"s":..}
            try:
                vx = float(data.get("vx", 0))
                vy = float(data.get("vy", 0))
                w  = float(data.get("w", 0))
            except (TypeError, ValueError):
                vx = vy = w = 0.0
            vx = max(-1.0, min(1.0, vx))
            vy = max(-1.0, min(1.0, vy))
            w  = max(-1.0, min(1.0, w))
            if abs(vx) < 0.02 and abs(vy) < 0.02 and abs(w) < 0.02:
                self.motor.stop()
                c = "stop"
            else:
                self.motor.drive(vx, vy, w, s)
                # 上报一个可读的方向名
                c = "move"
            self.vx, self.vy, self.w = vx, vy, w
        elif c == "forward":       self.motor.forward(s)
        elif c == "backward":    self.motor.backward(s)
        elif c == "left":        self.motor.spin_left(s)
        elif c == "right":       self.motor.spin_right(s)
        elif c == "spin_left":   self.motor.spin_left(s)
        elif c == "spin_right":  self.motor.spin_right(s)
        elif c == "strafe_left": self.motor.strafe_left(s)
        elif c == "strafe_right":self.motor.strafe_right(s)
        elif c == "brake":       self.motor.brake(); c="brake"
        else:                    self.motor.stop(); c="stop"
        self.dir = c
        print(f"[cmd] {c} speed={s}")

    def status(self):
        # 遥测
        bat_mv = read_adc_mv(self.adc_channel)
        tel = {"bat_mv": bat_mv,
               "bat_v": round(bat_mv * self.adc_ratio / 1000.0, 2) if bat_mv else None,
               "adc_ratio": self.adc_ratio}
        if self.gps:
            tel["gps"] = dict(self.gps.data)
        if self.check_4g:
            tel["net4g"] = check_4g()
        return {"mode":"sim" if self.sim else "live",
                "dir": self.dir, "speed": self.speed,
                "vx": getattr(self, 'vx', 0.0),
                "vy": getattr(self, 'vy', 0.0),
                "w": getattr(self, 'w', 0.0),
                "ts": int(time.time()), "online": True,
                "tel": tel}

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--broker", default=None)
    args = ap.parse_args()
    conf = load_conf()
    if args.broker: conf["mqtt"]["broker"] = args.broker
    sim = args.simulate

    car = Car(conf, sim)
    cid = "luckfox-car-" + uuid.uuid4().hex[:8]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)

    # 用户名/密码鉴权 (部署到自己 broker 时使用)
    m = conf["mqtt"]
    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password"))
    # TLS (若 broker 为 8883)
    if m.get("use_tls"):
        if m.get("ca_cert"):
            client.tls_set(ca_certs=m["ca_cert"])
        else:
            client.tls_set()

    def on_connect(c, u, flags, rc, p=None):
        print(f"[mqtt] 已连接 {m['broker']}:{m['port']} rc={rc}")
        c.subscribe(m["topic_cmd"])

    def on_message(c, u, msg):
        car.handle(msg.payload)

    def on_disconnect(c, u, flags, rc=None, p=None):
        print(f"[mqtt] 断开 rc={rc}, 重连中...")

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    print(f"[mqtt] 连接 {m['broker']}:{m['port']} cmd={m['topic_cmd']} auth={'on' if m.get('username') else 'off'}")
    client.connect(m["broker"], m["port"], 60)
    client.loop_start()

    last = 0
    try:
        while True:
            time.sleep(1)
            if time.time() - last >= m["heartbeat"]:
                client.publish(m["topic_status"], json.dumps(car.status()), qos=0)
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        car.motor.close()
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TB6612 四路电机驱动 MD240A (双 TB6612, 4WD 麦克纳姆轮) - Luckfox Pico Pro Max

硬件 PWM 版 (2026-10-07)
========================
方向仍用 GPIO (IN1/IN2), 调速改用 **RV1106 内置 PWM 控制器** (sysfs)。

为什么改硬件 PWM
----------------
旧方案是 Python 线程做软件 PWM: 单核 Cortex-A7 上每秒被唤醒 250 次,
每轮做多次 GPIO ioctl 并抢 GIL。改硬件 PWM 后占空比只写一次寄存器,
内核硬件自动输出波形, 用户态 **零线程、零轮询**, 只在实际变速时写一次 sysfs。

通道映射 (用户实测接线)
-----------------------
    FL 左前 -> gpio 54 -> pwm10m1 -> /sys/class/pwm/pwmchip10/pwm0
    FR 右前 -> gpio 73 -> pwm6m1  -> /sys/class/pwm/pwmchip6/pwm0
    BL 左后 -> gpio 71 -> pwm4m1  -> /sys/class/pwm/pwmchip4/pwm0
    BR 右后 -> gpio 58 -> pwm0m1  -> /sys/class/pwm/pwmchip0/pwm0

注意: 这些 pwmchip 只在 overlay 加载后才存在, 见 /userdata/hwcfg/pwm4.dtbo
      开机由 /etc/init.d/S97pwm 自动加载。
"""
import os, time, threading

PWM_ROOT = "/sys/class/pwm"


class HwPwm:
    """单个硬件 PWM 通道 (sysfs)。

    记录上次写入的 duty/enable, 值没变就不写 sysfs —— 电机控制里同一
    占空比会被反复下发 (心跳、重复指令), 省掉这些写入是必要的: 每次
    sysfs 写都是一次系统调用 + 驱动寄存器操作, 单核板上不便宜。
    """

    def __init__(self, chip, freq=1000.0):
        self.path = "%s/pwmchip%d/pwm0" % (PWM_ROOT, chip)
        self.chip = chip
        self.freq = freq
        self.period_ns = int(1e9 / freq)
        self._last_duty = None
        self._last_enable = None
        self.ok = False
        self.err = ""

    def _w(self, name, val):
        try:
            with open(os.path.join(self.path, name), "w") as f:
                f.write(str(val))
            return True
        except Exception as e:
            self.err = "%s: %s" % (name, e)
            return False

    def export(self):
        chipdir = "%s/pwmchip%d" % (PWM_ROOT, self.chip)
        if not os.path.isdir(chipdir):
            self.err = "pwmchip%d 不存在 (overlay 没加载?)" % self.chip
            return False
        if not os.path.isdir(self.path):
            try:
                with open(os.path.join(chipdir, "export"), "w") as f:
                    f.write("0")
            except Exception as e:
                self.err = "export: %s" % e
                return False
            for _ in range(50):          # 内核建目录需要一点时间
                if os.path.isdir(self.path):
                    break
                time.sleep(0.01)
        if not os.path.isdir(self.path):
            self.err = "export 后 %s 仍未出现" % self.path
            return False
        self.ok = True

        # ---- 极性必须显式设成 normal ----
        #
        # ⚠️ 这块板子上 Rockchip PWM 的**默认极性是 inversed**, 也就是说
        # duty_cycle 表示的是"低电平时间"。实测四路 (pwmchip10/11/6/8) 开机后
        # 全是 polarity=inversed。
        #
        # 而我们的调速按"duty 越大越快"来写 (set_speed: duty = 速度值%)。
        # 极性一反, 整条速度轴就镜像了:
        #     速度值 10  -> duty 10% -> 实际高电平 90%  -> 飞快
        #     速度值 100 -> duty 100% -> 实际高电平 0%   -> 停住
        # 用户报的"滑条往右拖数字变大反而更慢"就是这个, 代码里怎么查都是对的。
        #
        # 必须在 enable **之前**做: PWM 一旦使能, 多数驱动的 polarity 写入会
        # 直接返回 -EBUSY。而 web_server 重启时通道可能还是上一次留下的
        # "已导出且已使能" 状态, 所以先显式关掉再改极性, 否则这个修复会
        # 只在冷启动生效、重启服务就失效 —— 那种"时好时坏"最难查。
        if self._w("enable", 0):
            self._last_enable = 0
        if not self._w("polarity", "normal"):
            # 不致命: 只是速度轴向会反, 电机仍可控。留痕便于排查。
            print("[motor] !! pwmchip%d 极性设置失败: %s" % (self.chip, self.err))

        self._w("period", self.period_ns)   # period 必须先于 duty
        self._w("duty_cycle", 0)
        return True

    def set_duty(self, pct):
        """pct: 0..100"""
        if not self.ok:
            return False
        pct = max(0.0, min(100.0, float(pct)))
        duty_ns = int(self.period_ns * pct / 100.0)
        if pct >= 100.0:
            duty_ns = self.period_ns
        if self._last_duty == duty_ns:
            return True                      # 值没变, 不碰 sysfs
        if not self._w("duty_cycle", duty_ns):
            return False
        self._last_duty = duty_ns
        self.set_enable(duty_ns != 0)
        return True

    def set_enable(self, on):
        if not self.ok:
            return False
        on = 1 if on else 0
        if self._last_enable == on:
            return True
        if not self._w("enable", on):
            return False
        self._last_enable = on
        return True

    def read_back(self):
        """回读实际寄存器值, 用于验证 (duty_cycle, enable, polarity)。

        polarity 也回读: 它是"速度轴向对不对"的唯一硬件证据 —— 软件那边
        duty 和速度值成正比是看不出问题的 (见 export() 里的说明)。
        """
        out = {}
        for k in ("period", "duty_cycle", "enable", "polarity"):
            try:
                out[k] = open(os.path.join(self.path, k)).read().strip()
            except Exception as e:
                out[k] = "ERR:%s" % e
        return out

    def close(self):
        if not self.ok:
            return
        try:
            self.set_duty(0)
            self.set_enable(False)
        except Exception:
            pass


class FourMotor:
    CH = ('FL', 'FR', 'BL', 'BR')

    # 通道 -> pwmchip 号 (由 pinctrl 决定, 见模块 docstring)
    DEFAULT_PWM_CHIP = {'FL': 10, 'FR': 6, 'BL': 4, 'BR': 0}

    def __init__(self, pin, simulate=False, inv=None, freq=1000.0,
                 pwm_chip=None, use_hw_pwm=True):
        """
        pin: {"STBY": g, "FL": {"IN1":g,"IN2":g,"PWM":g}, ...}
             PWM 字段在硬件 PWM 模式下不使用 (保留以兼容旧 config)。
        pwm_chip: {CH: chipnum}, 默认 DEFAULT_PWM_CHIP。
        use_hw_pwm: False 时退回旧的软件 PWM (应急/调试)。
        """
        from periphery import GPIO

        self.pin = pin
        self.simulate = simulate
        self.use_hw_pwm = use_hw_pwm
        inv = inv or {}
        self.INV_VX = -1.0 if inv.get("vx") else 1.0
        self.INV_VY = -1.0 if inv.get("vy") else 1.0
        self.INV_W = -1.0 if inv.get("w") else 1.0

        self._g = {}
        self._duty = {c: 0.0 for c in self.CH}
        self._pwm = {}
        self._pwm_run = False
        self._pwm_thread = None
        self._last_dir = {}
        self._freq = freq if use_hw_pwm else 250.0
        self._period = 1.0 / self._freq

        self.dbg = {
            "calls": 0,
            "set_speed": 0,
            "dir_writes": 0,
            "pwm_writes": 0,      # 真正写 sysfs duty 的次数
            "pwm_skipped": 0,     # 因值没变而跳过的次数
            "mode": "hw" if use_hw_pwm else "sw",
            "pwm_alive": False,
            "pwm_err": 0,
            "last": None,
            "last_duty": {},
            "last_dir": {},
            "chips": {},
            "err": "",
            "t0": time.time(),
        }

        if simulate:
            print("[FourMotor] SIMULATE 模式, 不操作硬件")
            return

        # ---- GPIO: 方向 + STBY ----
        for c in self.CH:
            for k in ('IN1', 'IN2'):
                self._g[(c, k)] = GPIO(pin[c][k], 'out')
        self._g[('STBY',)] = GPIO(pin['STBY'], 'out')
        self._g[('STBY',)].write(True)

        # ---- 硬件 PWM ----
        if use_hw_pwm:
            chip = pwm_chip or self.DEFAULT_PWM_CHIP
            for c in self.CH:
                p = HwPwm(chip[c], freq=self._freq)
                if p.export():
                    self._pwm[c] = p
                    self.dbg["chips"][c] = chip[c]
                else:
                    self.dbg["err"] = "%s: %s" % (c, p.err)
                    print("[FourMotor] PWM %s (chip%d) 失败: %s"
                          % (c, chip[c], p.err))
            self.dbg["pwm_alive"] = len(self._pwm) == len(self.CH)

    # ---- 软件 PWM (仅 use_hw_pwm=False 时使用) ----
    def _pwm_loop(self):
        """旧的软件 PWM, 保留作降级方案。

        ⚠️ 这条路径是 CPU 大户: 每周期唤醒、每通道 GPIO write、抢 GIL。
        """
        n = len(self.CH)
        gs = [self._g.get((c, 'PWM')) for c in self.CH]
        last = [-1] * n
        dbg = self.dbg
        cur = [0.0] * n
        flip_idx = [0] * n
        flip_at = [0.0] * n
        nf = 0
        dbg["pwm_alive"] = True
        period = self._period
        while self._pwm_run:
            try:
                t0 = time.perf_counter()
                duty = self._duty
                all_idle = True
                for i in range(n):
                    d = duty[self.CH[i]]
                    cur[i] = d
                    if d > 0.0:
                        all_idle = False
                if all_idle:
                    for i in range(n):
                        if gs[i] is not None and last[i] != 0:
                            gs[i].write(False); last[i] = 0
                    time.sleep(0.05)
                    continue
                nf = 0
                for i in range(n):
                    d = cur[i]
                    if gs[i] is None:
                        continue
                    if d <= 0.0:
                        if last[i] != 0:
                            gs[i].write(False); last[i] = 0
                    elif d >= 1.0:
                        if last[i] != 1:
                            gs[i].write(True); last[i] = 1
                    else:
                        if last[i] != 1:
                            gs[i].write(True); last[i] = 1
                        at = t0 + period * d
                        a = nf
                        while a > 0 and flip_at[a - 1] > at:
                            flip_at[a] = flip_at[a - 1]
                            flip_idx[a] = flip_idx[a - 1]
                            a -= 1
                        flip_at[a] = at
                        flip_idx[a] = i
                        nf += 1
                for a in range(nf):
                    dt = flip_at[a] - time.perf_counter()
                    if dt > 0:
                        time.sleep(dt)
                    i = flip_idx[a]
                    if last[i] != 0:
                        gs[i].write(False); last[i] = 0
                rest = period - (time.perf_counter() - t0)
                if rest > 0:
                    time.sleep(rest)
            except Exception as e:
                dbg["pwm_err"] += 1
                dbg["err"] = "%s: %s" % (type(e).__name__, e)
                time.sleep(0.05)
        dbg["pwm_alive"] = False

    def start_pwm(self):
        if self.use_hw_pwm or self.simulate or self._pwm_run:
            return
        self._pwm_run = True
        self._pwm_thread = threading.Thread(target=self._pwm_loop, daemon=True)
        self._pwm_thread.start()

    def stop_pwm(self):
        self._pwm_run = False

    def pwm_state(self):
        """回读四路 PWM 实际寄存器值 (调试/验证用)。"""
        out = {}
        for c in self.CH:
            p = self._pwm.get(c)
            out[c] = p.read_back() if p else {"err": "not exported"}
        return out

    # ---- 方向控制 ----
    def _set_dir(self, d):
        if self.simulate:
            print("[FourMotor]", {c: d[c] for c in self.CH})
            return
        for c in self.CH:
            in1, in2 = d[c]
            if self._last_dir.get(c) == (in1, in2):
                continue                     # 方向没变就不碰 GPIO
            self._g[(c, 'IN1')].write(bool(in1))
            self._g[(c, 'IN2')].write(bool(in2))
            self._last_dir[c] = (in1, in2)
            self.dbg["dir_writes"] += 1
        self.dbg["last_dir"] = {c: list(self._last_dir.get(c, (0, 0)))
                                for c in self.CH}

    def set_speed(self, **kw):
        """设置四路占空比。硬件 PWM 下每路最多一次 sysfs 写 (值没变则跳过)。"""
        for c in self.CH:
            s = max(0.0, min(100.0, kw.get(c, 0)))
            self._duty[c] = s / 100.0
            if not self.simulate and self.use_hw_pwm:
                p = self._pwm.get(c)
                if p is not None:
                    before = p._last_duty
                    if p.set_duty(s):
                        if p._last_duty != before:
                            self.dbg["pwm_writes"] += 1
                        else:
                            self.dbg["pwm_skipped"] += 1
        self.dbg["set_speed"] += 1
        self.dbg["last_duty"] = {c: round(self._duty[c], 4) for c in self.CH}

    # ---- 4WD 麦克纳姆轮运动模型 ----
    def _apply(self, mapping, speed):
        d = {}
        for c in self.CH:
            m = mapping.get(c, 0)
            if m == 1:    d[c] = (1, 0)
            elif m == -1: d[c] = (0, 1)
            else:         d[c] = (0, 0)
        self._set_dir(d)
        self.set_speed(FL=speed.get('FL', 0), FR=speed.get('FR', 0),
                       BL=speed.get('BL', 0), BR=speed.get('BR', 0))

    # ---- 连续向量控制 (麦克纳姆轮运动学) ----
    def drive(self, vx=0.0, vy=0.0, w=0.0, speed=30):
        """按速度向量驱动四轮。

        vx 横移(+右/-左), vy 前后(+前/-后), w 自转(+左), speed 上限%

            FL = vy + vx + w
            FR = vy - vx - w
            BL = vy - vx + w
            BR = vy + vx - w
        """
        vx = vx * self.INV_VX
        vy = vy * self.INV_VY
        w = w * self.INV_W

        fl = vy + vx + w
        fr = vy - vx - w
        bl = vy - vx + w
        br = vy + vx - w

        m = max(abs(fl), abs(fr), abs(bl), abs(br))
        if m > 1.0:
            fl, fr, bl, br = fl / m, fr / m, bl / m, br / m

        k = max(0.0, min(100.0, float(speed))) / 100.0
        vals = {'FL': fl * k, 'FR': fr * k, 'BL': bl * k, 'BR': br * k}

        d = {}
        sp = {}
        for c in self.CH:
            v = max(-1.0, min(1.0, vals[c]))
            if v > 0.002:
                d[c] = (1, 0); sp[c] = abs(v) * 100.0
            elif v < -0.002:
                d[c] = (0, 1); sp[c] = abs(v) * 100.0
            else:
                d[c] = (0, 0); sp[c] = 0.0
        self._set_dir(d)
        self.set_speed(**sp)
        self.dbg["calls"] += 1
        self.dbg["last"] = {"vx": vx, "vy": vy, "w": w,
                            "speed": speed, "t": time.time()}

    def stop(self, speed=0):
        self._apply({}, {'FL': 0, 'FR': 0, 'BL': 0, 'BR': 0})
        self.set_speed()

    def brake(self, speed=0):
        d = {c: (1, 1) for c in self.CH}
        self._set_dir(d)
        self.set_speed(FL=0, FR=0, BL=0, BR=0)

    def forward(self, speed=30):
        self._apply({'FL': 1, 'FR': 1, 'BL': 1, 'BR': 1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def backward(self, speed=30):
        self._apply({'FL': -1, 'FR': -1, 'BL': -1, 'BR': -1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def spin_left(self, speed=30):
        self._apply({'FL': 1, 'FR': -1, 'BL': 1, 'BR': -1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def spin_right(self, speed=30):
        self._apply({'FL': -1, 'FR': 1, 'BL': -1, 'BR': 1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def strafe_left(self, speed=30):
        self._apply({'FL': -1, 'FR': 1, 'BL': 1, 'BR': -1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def strafe_right(self, speed=30):
        self._apply({'FL': 1, 'FR': -1, 'BL': -1, 'BR': 1},
                    {'FL': speed, 'FR': speed, 'BL': speed, 'BR': speed})

    def forward_left(self, speed=30):
        self._apply({'FL': 1, 'FR': 1, 'BL': 1, 'BR': 1},
                    {'FL': speed * 0.7, 'FR': speed, 'BL': speed,
                     'BR': speed * 0.7})

    def forward_right(self, speed=30):
        self._apply({'FL': 1, 'FR': 1, 'BL': 1, 'BR': 1},
                    {'FL': speed, 'FR': speed * 0.7, 'BL': speed * 0.7,
                     'BR': speed})

    def close(self):
        self.stop_pwm()
        if self.simulate:
            return
        if self.use_hw_pwm:
            for p in self._pwm.values():
                try: p.close()
                except Exception: pass
        for g in self._g.values():
            try: g.close()
            except Exception: pass

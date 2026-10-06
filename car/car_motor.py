#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TB6612 四路电机驱动 MD240A (双 TB6612 芯片, 4WD 麦克纳姆轮) - Luckfox Pico Max
使用 python-periphery 控制 GPIO。
方向用 GPIO 高低电平, 调速用软件 PWM (threading)。

4 路电机: FL(左前) FR(右前) BL(左后) BR(右后)
每个通道: {IN1, IN2, PWM}   (TB6612: IN1/IN2 方向, PWM 调速)
需要一个 STBY (待机) 引脚拉高使能。

引脚为占位值, 接线后修改 /etc/luckfox-car/car_config.json 里的 pin。
"""
import threading, time

class FourMotor:
    CH = ('FL', 'FR', 'BL', 'BR')

    def __init__(self, pin, simulate=False):
        """
        pin: dict
          {
            "STBY": int,
            "FL": {"IN1":g, "IN2":g, "PWM":g},
            "FR": {"IN1":g, "IN2":g, "PWM":g},
            "BL": {"IN1":g, "IN2":g, "PWM":g},
            "BR": {"IN1":g, "IN2":g, "PWM":g},
          }
        simulate: True 时不操作硬件 (用于无硬件测试)
        """
        from periphery import GPIO
        self.pin = pin
        self.simulate = simulate
        self._g = {}
        self._duty = {c: 0.0 for c in self.CH}
        self._pwm_run = False
        self._pwm_thread = None
        # 方向脚的上次写入值。_set_dir() 原来无条件写全部 8 个 GPIO, 而方向
        # 在一次操作里往往没变 (例如持续前进时每条 move 指令都是同一组方向),
        # 这些写操作在单核板上要和 HTTP 线程抢 GIL, 直接推高控制延迟。
        self._last_dir = {}
        # PWM 频率: 原来是 1000Hz。单核 Cortex-A7 上这个线程每秒醒 1000 次、
        # 做 ~8 次 GPIO ioctl 并抢 GIL, 实测把 web_server 的本地响应从个位数
        # ms 抬到 30ms、系统 load average 到 11.8。250Hz 对直流电机调速完全
        # 够用 (TB6612 支持到 100kHz, 电机机械时间常数远大于 4ms), 而 GIL
        # 抢占降到 1/4。代价是电机会有轻微可闻的哼声。
        self._freq = 250.0
        self._period = 1.0 / self._freq

        # ---- 运行时可观测性 (2026-10-05 加) ----
        # "按了没反应" 的排查必须有这几个数: 指令到底有没有走到 GPIO?
        # PWM 循环还活着吗? 有没有被异常打死? 见 /api/motordbg。
        self.dbg = {
            "calls": 0,          # drive()/forward()/... 被调用次数
            "set_speed": 0,      # set_speed() 次数
            "dir_writes": 0,     # 真正写方向 GPIO 的次数 (缓存跳过的不算)
            "pwm_iters": 0,      # PWM 主循环轮数
            "pwm_idle": 0,       # "全停空转"轮数
            "pwm_edges": 0,      # PWM 拉高次数 —— 有调制在跑的直接证据
            "pwm_err": 0,        # PWM 循环里的异常次数 (线程死了会体现在这里)
            "pwm_alive": False,
            "last": None,        # 最近一次 drive 的入参
            "last_duty": {},     # 最近一次占空比快照
            "last_dir": {},      # 最近一次方向快照
            "err": "",
            "t0": time.time(),
        }

        if not simulate:
            for c in self.CH:
                for k in ('IN1', 'IN2', 'PWM'):
                    self._g[(c, k)] = GPIO(pin[c][k], 'out')
            self._g[('STBY',)] = GPIO(pin['STBY'], 'out')
            self._g[('STBY',)].write(True)  # 使能
        else:
            print("[FourMotor] SIMULATE 模式, 不操作硬件")

    # ---- 软件 PWM ----
    def _pwm_loop(self):
        """四通道并行软件 PWM。

        修正 (2026-09-11): 原实现把 4 个通道串行放在一个循环里, 每个通道
        里 sleep(on)+sleep(off) 走完整个周期, 导致 4 通道时实际频率降为
        _freq/4, 且各通道相位错开、抖动大。改为按"同一时间基准"调度:
        每轮以 period 为界, 在周期开始拉高, 到位后拉低, 再统一睡眠到
        下一周期起点。这样 4 通道同步输出, 频率不随通道数下降。

        修正 (2026-09-23): 电机停着的时候这个循环仍以 1kHz 空转, 每轮对
        4 个 GPIO 各写一次 (4000 次/秒的无用写入), 实测吃掉 web_server
        25% 的 CPU —— 而板子是单核, 这些 CPU 本该给 SPI 隧道和视频编码。
        两个优化:
          1. 记住上次写过的值, 值没变就不碰 GPIO
          2. 四路都是 0 (全停) 时直接睡 50ms, 不做 1kHz 空转
        """
        gs = {c: self._g.get((c, 'PWM')) for c in self.CH}
        last = {c: None for c in self.CH}
        dbg = self.dbg

        def setg(c, v):
            g = gs[c]
            if g is None or last[c] == v:
                return
            g.write(v)
            last[c] = v
            if v:
                dbg["pwm_edges"] += 1

        dbg["pwm_alive"] = True
        while self._pwm_run:
            try:
                t0 = time.perf_counter()
                offs = {}
                all_idle = True
                for c in self.CH:
                    if gs[c] is None:
                        continue
                    d = self._duty[c]
                    if d <= 0.0:
                        setg(c, False)
                        continue
                    all_idle = False
                    if d >= 1.0:
                        setg(c, True)          # 满速: 常高, 不参与本轮翻转
                        continue
                    setg(c, True)
                    offs[c] = t0 + self._period * d
                # 到点逐个拉低 (按截止时间排序, 减少无谓比较)
                for c, toff in sorted(offs.items(), key=lambda kv: kv[1]):
                    dt = toff - time.perf_counter()
                    if dt > 0:
                        time.sleep(dt)
                    setg(c, False)

                dbg["pwm_iters"] += 1

                if all_idle:
                    # 全部停止: 没有需要调制的通道, 没必要 1kHz 空转
                    dbg["pwm_idle"] += 1
                    time.sleep(0.05)
                    continue

                # 统一对齐到下一周期起点
                rest = self._period - (time.perf_counter() - t0)
                if rest > 0:
                    time.sleep(rest)
            except Exception as e:
                # ⚠️ 这里以前**没有** try/except: 任何一次 GPIO 写失败
                # (瞬态 EIO/EBUSY) 都会让这个线程静默退出, 之后所有 duty
                # 更新都到不了引脚 —— API 照样返回 ok, 但车永远不动。
                # 现在计数 + 记录, 并且不让线程死。
                dbg["pwm_err"] += 1
                dbg["err"] = "%s: %s" % (type(e).__name__, e)
                time.sleep(0.05)
        dbg["pwm_alive"] = False

    def start_pwm(self):
        if self.simulate or self._pwm_run:
            return
        self._pwm_run = True
        self._pwm_thread = threading.Thread(target=self._pwm_loop, daemon=True)
        self._pwm_thread.start()

    def stop_pwm(self):
        self._pwm_run = False

    # ---- 方向控制 ----
    def _set_dir(self, d):
        # d: dict {CH: (in1, in2)}
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
        self.dbg["last_dir"] = {c: list(self._last_dir.get(c, (0, 0))) for c in self.CH}

    def set_speed(self, **kw):
        for c in self.CH:
            s = kw.get(c, 0)
            self._duty[c] = max(0, min(100, s)) / 100.0
        self.dbg["set_speed"] += 1
        self.dbg["last_duty"] = {c: round(self._duty[c], 4) for c in self.CH}

    # ---- 4WD 麦克纳姆轮运动模型 ----
    # 约定: 1=正转, 0=停, -1=反转 ; speed 为各轮 PWM%(0-100)
    def _apply(self, mapping, speed):
        # mapping: {CH: dir} dir in {-1,0,1}
        d = {}
        for c in self.CH:
            m = mapping.get(c, 0)
            if m == 1:   d[c] = (1, 0)
            elif m == -1:d[c] = (0, 1)
            else:        d[c] = (0, 0)
        self._set_dir(d)
        self.set_speed(FL=speed.get('FL',0), FR=speed.get('FR',0),
                       BL=speed.get('BL',0), BR=speed.get('BR',0))

    # ---- 连续向量控制 (麦克纳姆轮运动学) ----
    def drive(self, vx=0.0, vy=0.0, w=0.0, speed=30):
        """按速度向量驱动四轮, 实现平滑无级控制。

        vx : 横移, +右 / -左      (-1..1)
        vy : 前后, +前 / -后      (-1..1)
        w  : 自转, +逆时针/左转   (-1..1)
        speed : 总速度上限 % (0..100)

        麦轮标准运动学 (已与既有离散指令对齐: forward/backward/
        spin_left/spin_right/strafe_left/strafe_right 全部一致):
            FL = vy + vx + w
            FR = vy - vx - w
            BL = vy - vx + w
            BR = vy + vx - w
        归一化后再乘 speed, 保证任何方向组合都不会超过速度上限。
        """
        fl = vy + vx + w
        fr = vy - vx - w
        bl = vy - vx + w
        br = vy + vx - w

        # 归一化: 让最大分量不超过 1
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
                d[c] = (1, 0)          # 正转
                sp[c] = abs(v) * 100.0
            elif v < -0.002:
                d[c] = (0, 1)          # 反转
                sp[c] = abs(v) * 100.0
            else:
                d[c] = (0, 0)          # 停
                sp[c] = 0.0
        self._set_dir(d)
        self.set_speed(**sp)
        self.dbg["calls"] += 1
        self.dbg["last"] = {"vx": vx, "vy": vy, "w": w, "speed": speed, "t": time.time()}

    def stop(self, speed=0):
        self._apply({}, {'FL':0,'FR':0,'BL':0,'BR':0})
        self.set_speed()

    def brake(self, speed=0):
        # 全通道短接制动
        d = {c: (1, 1) for c in self.CH}
        self._set_dir(d)
        self.set_speed(FL=0,FR=0,BL=0,BR=0)

    def forward(self, speed=30):
        self._apply({'FL':1,'FR':1,'BL':1,'BR':1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    def backward(self, speed=30):
        self._apply({'FL':-1,'FR':-1,'BL':-1,'BR':-1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    # 原地左转 / 右转
    def spin_left(self, speed=30):
        self._apply({'FL':1,'FR':-1,'BL':1,'BR':-1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    def spin_right(self, speed=30):
        self._apply({'FL':-1,'FR':1,'BL':-1,'BR':1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    # 横移 (麦克纳姆轮)
    def strafe_left(self, speed=30):
        # 左前/右后 反转, 右前/左后 正转
        self._apply({'FL':-1,'FR':1,'BL':1,'BR':-1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    def strafe_right(self, speed=30):
        self._apply({'FL':1,'FR':-1,'BL':-1,'BR':1}, {'FL':speed,'FR':speed,'BL':speed,'BR':speed})

    # 斜向
    def forward_left(self, speed=30):
        self._apply({'FL':1,'FR':1,'BL':1,'BR':1}, {'FL':speed*0.7,'FR':speed,'BL':speed,'BR':speed*0.7})

    def forward_right(self, speed=30):
        self._apply({'FL':1,'FR':1,'BL':1,'BR':1}, {'FL':speed,'FR':speed*0.7,'BL':speed*0.7,'BR':speed})

    def close(self):
        self.stop_pwm()
        if not self.simulate:
            for g in self._g.values():
                try: g.close()
                except: pass

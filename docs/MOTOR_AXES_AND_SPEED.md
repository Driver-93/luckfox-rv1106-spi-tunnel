# 电机轴向与速度: 两个"软件全对但车不对"的坑

> 2026-10-08/09 用户报: "控制键左右是反的" + "速度小数值慢大数快应该"。
>
> 两次都是**软件侧怎么查都是对的**, 而车的行为是反的。记录在此, 免得下次刷机
> 或重建配置再踩。

---

## 1. 左右反了: `axis_inv` 里错误的 `vx` 取反

### 现象
摇杆往右拖 / 按 D / 按 →, 车往**左**走。前进后退正常。左转右转按钮正常。

### 根因
`/userdata/car/car_config.json`:

```json
"axis_inv": { "vx": true }
```

约定是单向的, 三处全都一致:
* `car_motor.drive()` 文档: `vx 横移(+右/-左)`
* `index.html` keyApply: `d/arrowright -> x += 1`
* `tools/diagnose/check-axis.py`: 右移 = `FL+ FR- BL- BR+`

再把 `vx` 取反, 整条左右轴就镜像了。

### 为什么我的配置里会有这个值
`board/config/car_config.example.json` (provisioning 模板) **里就写着
`{"vx": true}`** —— 上一轮刷机重建活配置时照抄了进去, 错误从模板一路传到车上。
**模板和活配置都改了**, 只改一个下次刷机又会回来。

### 怎么验证 (不动物理车)
`tools/diagnose/verify-axis-mapping.py` —— 用 `FourMotor(simulate=True)`,
在 `__init__` 里就 return, 完全不碰 GPIO/PWM, 轮子不会转。它把六个动作的四轮
方向位打出来, 对照 check-axis.py 的口径:

```
动作    修复前(vx=true)      修复后({})
前进    + + + +  OK         + + + +  OK
后退    - - - -  OK         - - - -  OK
右移    - + + -  ✗ 往左     + - - +  ✓
左移    + - - +  ✗ 往右     - + + -  ✓
右转    + - + -  OK         + - + -  OK
左转    - + - +  OK         - + - +  OK
```

### ⚠️ 别顺手"修正"左转右转
`index.html` 里 Q/E 那段注释是**实测结论**: 本车上 `w>0` 实际是**右转**,
与 `drive()` 文档字面 ("w+=左") 相反, 代码已按实测值校正过。文档字面是错的,
代码是对的 —— 动它之前先实测。

---

## 2. 速度反了: PWM 极性默认是 `inversed`

### 现象
速度滑条**往右拖 (数字变大) 车反而更慢**; 拉到 100 几乎不动。

### 根因
这块板子上 Rockchip PWM 的**默认极性是 `inversed`** —— `duty_cycle` 表示的是
**低电平时间**。而我们的调速模型是"duty = 速度值%"。极性一反:

```
速度值  10  -> duty 10%  -> 实际高电平 90%  -> 飞快
速度值 100  -> duty 100% -> 实际高电平  0%  -> 停住
```

### 为什么难查
`car_motor.set_speed` / `HwPwm.set_duty` 全部正确, 占空比确实随速度值单调上升
(用 `simulate=True` 实测过: 10->10%, 30->30%, 60->60%, 100->100%)。
**软件里没有任何一处反**, 日志也没有任何异常。唯一的痕迹在 sysfs:

```
/sys/class/pwm/pwmchip10/pwm0/polarity = inversed   (四路都是)
```

界面上的表现和代码完全对不上, 所以只能靠查硬件属性。

### 修法
`car_motor.HwPwm.export()` 里, **顺序**是功能性的:

```python
if self._w("enable", 0):        # 重启服务时通道可能还使能着
    self._last_enable = 0
self._w("period", self.period_ns)   # ⚠️ 必须在 polarity 之前!
self._w("polarity", "normal")
self._w("period", self.period_ns)   # 改极性后驱动可能重置 period, 补一次
self._w("duty_cycle", 0)
```

#### ⚠️ 为什么 period 必须在 polarity 之前 (踩了两次)

**内核的 `pwm_apply_state()` 开头就校验 `state->period < 1 -> -EINVAL`。**
而刚 `export` 出来的通道 `period` 是 **0**。所以 "先写 polarity" 会直接:

```
[motor] !! pwmchip10 极性设置失败: polarity: [Errno 22] Invalid argument
```

后果是极性问题**原样回来**, 而且:

* **只在真正的冷启动上出现**。已经跑起来的系统里通道早被上一次运行配好了
  period, 所以写得过 —— 我第一版"改完测一下"和"重启服务测一下"都是通过的。
  用户报的原话就是"一度正常, 重启后失效"。
* 重启服务 (`/etc/init.d/S23web restart`) **测不出这个 bug**, 必须冷启动,
  或者人为 `echo 0 > .../unexport` 把通道打回 period=0 再起服务。

#### ⚠️ 另一个坑: 别把 dbg 写到 HwPwm 上

回读极性时写 `self.dbg[...]` 会抛 `AttributeError` —— `dbg` 是 `FourMotor` 的,
`HwPwm` 没有。而它在 `export()` 中间, 于是**循环到第一路就断了**:

```
[motor] 启动失败: 'HwPwm' object has no attribute 'dbg'
```

结果是只有一个通道被导出、电机整个没起来。现在极性存在 HwPwm 自己的
`self.polarity` 上, 由 `FourMotor` 在 `__init__` 里汇总进 `dbg["polarity"]`。

#### ⚠️ 单个指令测不出 duty —— 0.5s 失控保护会先把它归零

`failsafe_s = 0.5`。发一条 `forward` 然后 `cat duty_cycle`, 等 wget 返回时
deadman 往往已经停车了, 读到的就是 `duty=0` —— **看起来像"指令没生效",
其实是保护正常工作**。我自己就被这个误导过一次。

要测就得: 临时把超时放宽 (`POST /api/failsafe {"t":2}`) → 发一条低速度指令 →
立刻读 sysfs → stop → 恢复 0.5s。或者干脆用 `FourMotor(simulate=False)` 直接
调 `drive()` 再 `pwm_state()` 回读, 不走 HTTP (见 `verify-motor.py`)。

### 怎么验证
| 工具 | 查什么 |
|---|---|
| `tools/diagnose/check-pwm-polarity.sh` | 四路极性必须全是 `normal` |
| `tools/diagnose/verify-motor.py` | 冷通道启动 + duty 是否随速度值严格成比例 |
| `/api/motordbg` 的 `dbg.polarity` | 四路实际极性, 从隧道外面也能看到 |

实测 (冷启动后, 走完整 HTTP 链路):

```
POST /api/cmd {"c":"forward","s":10}   -> {"ok":true,"dir":"forward"}
    四路 duty=100000 enable=1 polarity=normal     (period=1000000)
s=60 -> duty=600000
stop -> 四路 duty=0 enable=0
```

---

## 3. 顺带发现: 配置里的引脚和 dts 注释对不上

`board/dts/pwm4.dts` 的注释写的是 **2026-10-08 用户全部重接线后**的映射:

| | dts (重接线后) | 活配置 car_config.json |
|---|---|---|
| FL | pwm 57 / chip10, AIN 56/72 | chip10, IN1/IN2 56/72 ✓ |
| FR | pwm 55 / **chip11**, AIN **43/42** | **chip8**, IN1/IN2 **53/54** ✗ |
| BL | pwm 52 / **chip8**, AIN **53/54** | **chip6**, IN1/IN2 **58/59** ✗ |
| BR | pwm 73 / **chip6**, AIN **59/58** | **chip11**, IN1/IN2 **66/65** ✗ |

而 pinctrl 实测确认了 dts 的说法:
`pwmchip8=pin52, pwmchip11=pin55, pwmchip10=pin57, pwmchip6=pin73`。

**这两个说法不可能都对。** 目前车能正常前进后退, 说明活配置在实际使用中是
自洽的 (前进时四轮同向, chip/引脚错配看不出来), 但左右/转向就有可能不对 ——
如果之后发现**只有转向或横移反**, 优先怀疑这里, 而不是继续改 `axis_inv`。

判断方法: 把车**架起来**(轮子离地), 跑 `check-axis.py --test`, 逐个动作看轮子
转向; 或直接 `cat /sys/kernel/debug/pinctrl/*/pinmux-pins` 对齐 pwm 引脚。

**这个问题还没解决** —— 需要用户确认接线 (或以 dts 为准重写活配置)。

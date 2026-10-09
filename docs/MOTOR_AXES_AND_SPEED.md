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
`car_motor.HwPwm.export()` 里, **在 enable 之前**写 `polarity=normal`:

```python
if self._w("enable", 0):        # 重启服务时通道可能还使能着
    self._last_enable = 0       # polarity 在已使能时会返回 -EBUSY
if not self._w("polarity", "normal"):
    print("[motor] !! pwmchip%d 极性设置失败" % self.chip)
self._w("period", self.period_ns)   # period 必须先于 duty
self._w("duty_cycle", 0)
```

顺序很关键:
1. **先 enable=0** —— 否则只在冷启动生效, 重启 web 服务就失效 (最难查的那种)
2. **再 polarity** —— 使能状态下驱动会拒绝改极性
3. **最后 period / duty** —— 改极性后驱动会重置 period, 必须重设

### 怎么验证
`tools/diagnose/check-pwm-polarity.sh` —— 四路极性必须全是 `normal`。
`car_motor.HwPwm.read_back()` 现在也回读 `polarity`。

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

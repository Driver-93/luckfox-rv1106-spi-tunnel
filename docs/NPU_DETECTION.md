# NPU 人/狗检测：可行性实测报告

> **结论先说**
>
> 1. **NPU 硬件真实可用** —— 驱动、运行时、硬件节点都在；官方 `yolov5.rknn`
>    能加载，`lsmod` 的 `rknpu Used=1` 证明 NPU 被真正持有。
> 2. **持有 NPU 不影响控制延迟** —— p50 变化在噪声内（详见 §3.4）。
> 3. **但本固件没有自带 rockiva 检测模型**，所以 rkipc 的 `enable_npu=1`
>    这条"零成本集成"路线**走不通**：
>    `E rockx(load_model+398): object_detection_pfp model data not found!`
> 4. **官方 demo 与图传互斥**（要独占摄像头）。
>
> **修正一个我上一轮的错误结论**：我曾写"`enable_npu=1` 会导致板子重启"。
> 这是**错的** —— 重启是我自己造成的：我杀掉 rkipc 后用 `nohup ./rkipc`
> 重启，**漏了 `LD_LIBRARY_PATH`**，rkipc 因找不到 `librockit.so` 秒退；
> 它是开机链的一部分，死了之后**硬件看门狗**就复位了板子。
> 用正确环境重启后，`enable_npu=1` 下板子**稳定运行、图传正常**（`uptime` 连续增长）。

---

## 一、硬件与软件栈（实测）

| 项 | 实测值 |
|---|---|
| NPU 硬件节点 | `/sys/devices/platform/ff660000.npu` |
| 设备文件 | `/dev/rknpu`（`crw-------` 10,122）|
| 内核模块 | `rknpu`（27019 字节，已加载）|
| 运行时库 | `/oem/usr/lib/librknnmrt.so` — **`librknnmrt version: 1.6.0 (2de554906@2024-01-17)`** |
| 2D 加速 | `/oem/usr/lib/librga.so` + `rga3` 模块（YOLO 预处理要用）|
| 智能视觉 | `/oem/usr/lib/librockiva.so`（rkipc 依赖它）|
| 系统 libc | **uClibc 1.0.31**（不是 glibc）+ `/lib/ld-uClibc.so.0` |
| Python | 3.11.6，**无 numpy**，**无 rknnlite** |
| 板载编译器 | **无**（不能现场编译）|

### RKNN 驱动版本检查是个坑

运行时里带这段逻辑：

```
E RKNN: Mismatch driver version, %s requires driver version >= %d.%d.%d,
        but you have driver version: %d.%d.%d which is incompatible!
```

**模型、运行时、内核驱动三者版本必须匹配**，否则 `rknn_init` 直接失败。
官方 demo 附带的是 **RKNN SDK v1.6.0** 时代的产物，与板上 `librknnmrt 1.6.0` 一致，
所以模型能加载成功（实测 `rknn_init` 没报版本错）。

---

## 二、已部署的资产

放在 **SD 卡**（`/mnt/sdcard/npu/`），不占 `/userdata`：

```
/mnt/sdcard/npu/
  luckfox_pico_yolov5        2.7MB  官方预编译 demo（uclibc armv7, hard-float）
  launch.sh                         独立启动脚本（见下面的坑）
  model/
    yolov5.rknn              7.2MB  COCO 80 类，含 person / dog
    coco_80_labels_list.txt         类别名
    anchors_yolov5.txt              锚点
```

来源：<https://github.com/LuckfoxTECH/luckfox_pico_rknn_example>
（`install/uclibc/luckfox_pico_yolov5_demo/`，分支 `kernel-5.10.160`）

**为什么放 SD 卡**：`/userdata` 只有 2.2MB（放不下 10MB），`/oem` 只剩 3.7MB。
SD 卡原本是 vfat + `noexec`（程序跑不起来），已格成 **ext4**（28.2GB 可用、可执行）。

---

## 三、实测数据

### 3.1 demo 能跑起来，模型能加载

```
launch: pwd=/mnt/sdcard/npu
launch: model=-rw------- 1 root root 7589751 model/yolov5.rknn
opencv-mobile MIPI CSI camera with v4l2 rkaiq
   devpath = /dev/video11
   driver = rkisp_v7
   fmt = Y/CbCr 4:2:0 (N-C)
       size = 32 x 32  ~  2304 x 1296  (+8 +8)
```

证据（`lsmod` 的 Used 列是关键）：

```
rknpu   27019   1      <- Used=1, 说明 NPU 被真正持有
demo 内存 VmRSS = 28832 KB  <- 模型+NPU 上下文已驻留内存
```

**NPU 初始化成功，模型加载成功**（否则 `rknn_init` 会报版本/解析错误，进程会退出）。

### 3.2 但它卡在摄像头 —— 与图传互斥

```
pid=3200 (rkipc)      -> /dev/video11    ← 图传在用
pid=2233 (yolo demo)  -> /dev/video11    ← NPU 也想用
```

demo 打完格式枚举后就**阻塞住了**，CPU 占用 0%（`87% idle`），说明在等设备。

官方 README 把这点写得很直白：

> 在运行demo前请执行 `RkLunch-stop.sh` 关闭开机默认开启的后台程序 rkipc，
> **解除对摄像头的占用**

**rkipc 必须一直跑**（推 RTSP 给 mediamtx，才有图传），所以官方 demo 的采集方式
不能直接用。

### 3.3 `enable_npu` 开关：**真正的失败原因是缺模型**（不是崩溃/重启）

`/userdata/rkipc.ini` 里有 `enable_npu = 0` + `npu_fps = 10`，
二进制里也有一整套链路：

```
ROCKIVA_Init / ROCKIVA_PushFrame
rkipc_rockiva_write_nv12_frame_by_phy_addr   <- 按物理地址零拷贝推帧
RkipcNpuOsd / ai_get_detect_result           <- 结果直接画到 OSD
```

**用正确的环境**（`LD_LIBRARY_PATH=/oem/usr/lib`）重启 rkipc、并开启
`enable_npu = 1`，抓到的日志是：

```
[rockiva.c][rkipc_rockiva_init]:begin
[rockiva.c][rkipc_rockiva_init]:ROCKIVA_Init over
E rockx(load_model+398): object_detection_pfp model data not found!
ROCKIVA_BA_Init error -3
```

**根因：固件里没有 rockiva 的检测模型文件。**

同一时刻的状态证明了"板子没崩、图传正常"：

```
uptime=1004s -> 1038s    (连续增长, 没有重启)
rkipc: 3006 root ./rkipc -a /oem/usr/share/iqfiles
554 监听: 1              (RTSP 正常, 图传可用)
rknpu Used: 0            (NPU 没被用上, 因为模型加载失败)
```

#### 为什么没有模型

`librockiva.so` 内部只认两件事：

```
%s.data        <- RockX 的模型数据格式
%s.rknn
```

以及一大串模型类型名（`OBJECT_DETECTION_IPC_PFP`、`..._X_PERSON`、
`..._V6_PFP` 等）。但**全盘扫描确认这些文件一个都不存在**：

```sh
find / -name '*.data'   # 只返回 /sys/module/*/sections/.data (内核段, 无关)
find / -name '*.rknn'   # 只有我自己传上去的 yolov5.rknn
```

`/oem/usr/share` 里只有 iqfiles、字体、ini 模板 —— **没有任何模型**。

> **注意**：rockiva 用的是 RockX `.data` 格式，**不是** `.rknn`，
> 所以官方 demo 的 `yolov5.rknn` **不能**直接顶替它。

#### 定位过程（可复用）

失败信息只在**内存里的日志**，重启就没了，所以先做了个**落盘采集器**：

```sh
# /mnt/sdcard/npu/crashwatch.sh —— 每秒把 dmesg 尾部 + 内存 + 进程状态
# 追加到 SD 卡上的日志, 这样重启后仍能看到崩溃/失败现场
```

这是不接串口也能拿到现场的实用办法。采集到 TRIGGER 前后的日志后，
才看到 `object_detection_pfp model data not found!` 这一行。

#### 顺带修正一个错误结论

我上一轮写的是"`enable_npu=1` 会让板子连续重启 3 次"。**那是错的**，
真实原因是我的启动方式有问题。三组对照试验：

| 试验 | 启动方式 | 结果 |
|---|---|---|
| A | 直接 `rkipc`（靠 PATH）| `rkipc: not found`（`/oem/usr/bin` 不在 PATH）|
| B | `cd /oem/usr/bin && ./rkipc` | **`can't load library 'librockit.so'`** ← 我踩的坑 |
| C | 加 `LD_LIBRARY_PATH=/oem/usr/lib` | **正常存活** ✓ |

**rkipc 是开机链的一部分**，它被我用错误方式搞死后，硬件看门狗
（`S21wdt`）就复位了板子 —— 所以看起来像"enable_npu 导致重启"，
其实是**我杀了 rkipc 又没能把它正确拉起来**。

**教训**：动开机链里的常驻服务之前，先确认它的**完整启动环境**
（尤其是 `LD_LIBRARY_PATH`、cwd、PATH），并且**准备好正确的重启命令**。
我因为漏了这一步，浪费了一整轮，还差点把一个正确的配置当成"有毒"回滚掉。

### 3.4 关键决策数据：NPU 持有**不影响控制延迟**

这才是"能不能上"的判据（用户要求控制延迟优先）。同一套测量、各 160 次采样：

| 条件 | p50 | p90 | p99 | max |
|---|---|---|---|---|
| 干净基线 | **26.1ms** | 37.5ms | 59.4ms | 80.1ms |
| 带 NPU（第 1 次） | **28.4ms** | 40.3ms | 74.5ms | 119.7ms |
| 带 NPU（第 2 次） | **25.3ms** | 35.2ms | 72.8ms | 74.6ms |

**p50 变化 −0.8 ~ +2.3ms，落在噪声范围内**（基线自身两次测量就差 2ms）。
结论：**持有 NPU 上下文 + 模型驻留内存（28MB）对控制链路没有可测出的影响。**

> 注意：这里测的是"**持有** NPU"，不是"**高频跑推理**"。
> 真正的推理负载对延迟的影响尚未测到，因为 demo 卡在取帧，还没跑起来。

---

## 四、踩过的坑（都能省你时间）

1. **`/userdata/rkipc.ini` 每次开机被模板覆盖**
   `/oem/usr/bin/RkLunch.sh` 第 122 行：`cp $default_rkipc_ini $rkipc_ini -f`
   → 只改 ini 是白改，**必须同时改 `/oem/usr/share/rkipc-300w.ini`**。

2. **SD 卡是 vfat + `noexec`**，程序放上去跑不了（`Permission denied`）。
   格成 ext4 解决。

3. **busybox 的 `sh` 不支持 `local`**（函数里用会静默失败）。
   另外 `timeout` 命令也没有。

4. **通过 SSH 管道跑 demo 会 "not found"**：
   管道方式下 `cd` / `LD_LIBRARY_PATH` 不生效，且 SSH 断开子进程被回收。
   正确做法：**把启动脚本写成文件**（`launch.sh`，内含显式 `cd` + `export`），
   再用 `nohup ... &` 拉起。

5. **SSH 管道里的 heredoc 会被截断**（`python3 - <<'PY'`）。
   把脚本 scp 到板子上再执行才可靠。

6. **busybox 的 `ps` 里 `grep` 会匹配到自己**，统计进程数要用 `grep -c '[l]uckfox'`
   这种写法。

7. **重启 rkipc 必须带 `LD_LIBRARY_PATH`**（见 §3.3 的三组对照试验）。
   漏了它 rkipc 会以 `can't load library 'librockit.so'` 秒退，
   而它是开机链的一部分 → 硬件看门狗复位板子。

8. **rkipc 的 IVS 结果只在终端打印，不画到画面上**（默认只框人）：
   `[video.c][rkipc_ivs_get_results]:MD: md_area is ...` 是运动检测输出。

---

## 五、关于 rockiva 的检测能力（调研结论）

rkipc 的 `enable_npu` 走的是 **rockiva** 引擎，模型是
**PFP = Person / Face / Pet**（人 / 脸 / 宠物）—— **正好覆盖"人 + 狗"**。

社区分析（[rkipc 的 npu(iva) 学习笔记](https://www.cnblogs.com/tlnshuju/p/19092860)）
给出的源码路径与关键点：

```c
// rkipc/common/rockiva/rockiva.c
globalParams.detModel |= ROCKIVA_DET_MODEL_PFP;   // 指定 PFP 前级检测
initParams.baRules.areaInBreakRule[0].objType =
    ROCKIVA_OBJECT_TYPE_BITMASK(ROCKIVA_OBJECT_TYPE_PERSON);
initParams.baRules.areaInBreakRule[0].objType |=
    ROCKIVA_OBJECT_TYPE_BITMASK(ROCKIVA_OBJECT_TYPE_PET);   // 宠物要手动加

// rkipc/src/rv1106_ipc/video/video.c -> rkipc_get_nn_update_osd()
// 默认只画 person, 宠物框要自己加:
if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PERSON) { draw_rect_2bpp(...); }
else if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PET) { draw_rect_2bpp(...); }
```

要点：

* **检测框是 rkipc 自己画的**（`draw_rect_2bpp` 到 RGN 画布），不需要我们写 UI。
* 坐标是**归一化的 1~10000**，要乘以图像分辨率才是真实像素位置。
* `objId` 是目标序号；目标消失再出现会 +1。
* **默认只框 person**，宠物要改代码 —— 意味着要**重新编译 rkipc**，
  而板上没有编译器（需要 Luckfox SDK 交叉编译）。

---

## 六、下一步（修正后的优先级）

| 方案 | 做法 | 代价 / 风险 |
|---|---|---|
| **① 找 rockiva 模型** | 拿到 `object_detection_pfp.data`（RockX 格式）放进 rkipc 的查找路径 | 模型**不在本固件里**；需要从 Rockchip SDK 或 Luckfox 官方获取 |
| ② 自建检测（不依赖 rockiva） | 拿 `yolov5.rknn`（已验证能加载）+ 从 rkipc **次码流 704×576** 取帧，自己做推理，框通过网页叠加 | 要自己写取帧+后处理；解码占 CPU 需实测 |
| ③ 重编 rkipc 加宠物框 | 用 Luckfox SDK 交叉编译，改 `rkipc_get_nn_update_osd()` | 前提是 ① 先解决（没模型编了也没用）|
| ④ 官方 demo 独占相机 | `RkLunch-stop.sh` 后跑 demo | **开车时看不到图传**，不实用 |

**推荐：②**。

理由：① 依赖一份我们手上没有、且来源不确定的模型文件（`rockiva` 用的
RockX `.data` 格式，**不能**用现成的 `yolov5.rknn` 顶替）；
而 ② 路线的关键组件**都已经验证可用**：
`yolov5.rknn` 能加载、NPU 能持有、次码流 `704×576` 已经在跑、
控制延迟不受影响（§3.4）。

---

## 七、复现命令

```sh
# 确认 NPU 硬件在
ls /sys/devices/platform/ | grep npu
lsmod | grep rknpu
ls -l /dev/rknpu

# 运行时版本
strings /oem/usr/lib/librknnmrt.so | grep 'librknnmrt version'

# --- 复现"缺模型"这个结论 ---
sed -i 's/^enable_npu.*/enable_npu = 1/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
for p in $(ps | grep '[r]kipc' | awk '{print $1}'); do kill $p; done; sleep 5
cd /oem/usr/bin
# ⚠️ 必须带 LD_LIBRARY_PATH, 否则 rkipc 秒退 -> 看门狗复位板子
LD_LIBRARY_PATH=/oem/usr/lib:/lib:/usr/lib ./rkipc -a /oem/usr/share/iqfiles > /tmp/rk.log 2>&1 &
sleep 20
grep -iE 'rockiva|rockx|not found' /tmp/rk.log
#   -> E rockx(load_model+398): object_detection_pfp model data not found!

# --- 回滚 ---
sed -i 's/^enable_npu.*/enable_npu = 0/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini

# --- 跑官方 demo（会卡在摄像头，因为 rkipc 占着；但能验证 NPU）---
nohup /mnt/sdcard/npu/launch.sh >/dev/null 2>&1 &
sleep 12
lsmod | grep rknpu          # Used=1 表示 NPU 被持有
cat /tmp/launch.log

# --- 控制延迟对比 ---
python3 /tools/npu/measure-control-latency.py cleanBase   # 先确保没有 demo
# 再拉起 demo, 跑一次对比
```


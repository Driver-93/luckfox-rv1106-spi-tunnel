# NPU 人/狗检测：可行性实测报告

> **结论先说**：这块板子的 **NPU 是真实可用的**（驱动、运行时、硬件节点都在，
> 模型能加载，`/dev/rknpu` 能被打开并计入 `lsmod` 的 Used）。
> 但**官方 demo 与图传互斥**，而 rkipc 自带的 `enable_npu` 开关在**本固件上会
> 导致重启**且实际不启用 NPU。
>
> 因此本文记录的是**做到哪一步、卡在哪、以及为什么**，而不是一个已上线的功能。

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

### 3.3 `enable_npu` 开关：会导致重启，且不生效

`/userdata/rkipc.ini` 里有 `enable_npu = 0`（第 26 行）+ `npu_fps = 10`，
二进制里也有一整套链路：

```
ROCKIVA_Init / ROCKIVA_PushFrame
rkipc_rockiva_write_nv12_frame_by_phy_addr   <- 按物理地址零拷贝推帧
RkipcNpuOsd / ai_get_detect_result           <- 结果直接画到 OSD
```

看上去正是我们要的。**但实测把它改成 1 之后**：

| 现象 | 结果 |
|---|---|
| 板子重启 | **连续 3 次**（`up 1 min` 反复出现）|
| NPU 是否被 rkipc 打开 | **没有**（`/proc/<rkipc>/fd` 里没有 `rknpu`）|
| 温度 | 49.9°C（正常，不是过热）|
| rkipc 日志 | 被重启清空，抓不到崩溃原因 |

**已全部回滚**（`ini` 与模板都改回 0，`diff` 确认与备份一致）。

> **推测**（未证实）：`rockiva` 需要配套的模型/数据文件，但 `find / -name '*.rknn'`
> 在 `/oem` 下一个都没找到 —— 缺模型可能导致初始化失败进而崩溃。
> 要证实需要**串口日志**（重启后 dmesg 会被清空，看不到崩溃现场）。

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

---

## 五、下一步（按代价排序）

| 方案 | 做法 | 代价 / 风险 |
|---|---|---|
| **① 抓串口日志定位重启原因** | 接串口，开 `enable_npu=1`，看崩溃现场 | 需要接线；但这是唯一能证实/证伪的方向 |
| ② 找 rockiva 配套模型 | 查该固件版本是否需要额外 `.rknn` 放进 `/oem` | 需要知道 Rockchip 的约定路径 |
| ③ 旁路取帧（不抢相机） | 从 rkipc 的**次码流 704×576**（已在跑）拉流解码喂 NPU | 要自己写取帧；解码占 CPU，需实测 |
| ④ 官方 demo 独占相机 | `RkLunch-stop.sh` 后跑 demo | **开车时看不到图传**，不实用 |

**推荐顺序：① → ②**。先看崩溃现场，避免继续盲试；
如果确实缺模型，② 就能解决。

---

## 六、复现命令

```sh
# 确认 NPU 硬件在
ls /sys/devices/platform/ | grep npu
lsmod | grep rknpu
ls -l /dev/rknpu

# 运行时版本
strings /oem/usr/lib/librknnmrt.so | grep 'librknnmrt version'

# 跑 demo（会卡在摄像头，因为 rkipc 占着）
nohup /mnt/sdcard/npu/launch.sh >/dev/null 2>&1 &
sleep 12
lsmod | grep rknpu          # Used=1 表示 NPU 被持有
cat /tmp/launch.log

# 控制延迟对比
python3 /tmp/lat.py cleanBase          # 先确保没有 demo
# 对比时把 demo 拉起来再跑一次
```

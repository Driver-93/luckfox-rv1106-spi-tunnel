# NPU 人/狗检测：已上线（实测报告）

> **状态：可用，已持久化（重启自动恢复）**
>
> 这块板子的 NPU 现在真的在跑 **人 / 脸 / 宠物** 检测，检测框由 rkipc 自己
> 画到视频流上 —— **不需要另起进程抢摄像头，不需要自己写解码器**。

---

## 一、最终配置（就这么三件事）

| 项 | 值 | 说明 |
|---|---|---|
| `/usr/lib/object_detection_pfp.data` | 1.0 MB | **关键**：rockiva 的检测模型，固件里原本没有 |
| `/usr/lib/object_detection_pfp_896x512.data` | 1.0 MB | 同上（备用分辨率）|
| `/userdata/rkipc.ini` + `/oem/usr/share/rkipc-300w.ini` | `enable_npu = 1`<br>`npu_fps = 2` | 开启检测；帧率降到 2 保控制延迟 |

改完用 `video_ctl._restart_rkipc()` 重启，然后 **重启板子验证过持久化**。

---

## 二、怎么找到"缺模型"这个根因的

### 2.1 现象

开 `enable_npu = 1` 后 rkipc 报：

```
[rockiva.c][rkipc_rockiva_init]:begin
[rockiva.c][rkipc_rockiva_init]:ROCKIVA_Init over
E rockx(load_model+398): object_detection_pfp model data not found!
ROCKIVA_BA_Init error -3
```

**NPU 没被用上**（`rknpu Used=0`），但**板子没崩、图传正常** —— 只是检测初始化失败。

### 2.2 抓现场的办法（不接串口）

重启会清空内存里的 dmesg，所以做了个**落盘采集器**
（`tools/npu/watch-crash-log.sh`）：每秒把 dmesg 尾部 + 内存 + 进程状态
追加到 **SD 卡**（重启后仍在）。这是不接串口也能拿到失败现场的关键。

### 2.3 定位到路径

rkipc 源码 `common/rockiva/rockiva.c` 第 201 行：

```c
snprintf(globalParams.modelPath, ROCKIVA_PATH_LENGTH, "/usr/lib/");
```

→ **模型要放 `/usr/lib/`**。而板子上 `/usr/lib/` 里一个 `.data` 都没有。

### 2.4 模型从哪来

Luckfox SDK 里就有（正好是 rv1106 版本）：

```
media/iva/iva/models/rockiva_data_rv1106/object_detection_pfp.data
media/iva/iva/models/rockiva_data_rv1106/object_detection_pfp_896x512.data
```

拷到板上 `/usr/lib/` 即可。**板子 rootfs 有 110MB 余量，两个文件占 2MB，没问题。**

> **注意**：rockiva 用的是 RockX `.data` 格式，**不是** `.rknn`。
> 网上能找到的 `yolov5.rknn` **不能**替代它。

---

## 三、实测数据

### 3.1 检测确实在跑

```
rknpu Used = 1                          <- NPU 被 rkipc 持有
fd -> /dev/rknpu
[检测线程] iva_main_loop                <- rockiva 主推理循环
[检测线程] RkipcGetIVS                  <- IVS 结果消费
[检测线程] RkipcNpuOsd                  <- 把框画到 OSD
[rockiva.c] ROCKIVA_BA_Init success     <- 初始化成功
'model data not found' 次数 = 0         <- 之前是致命错误
```

rkipc 内存 `VmRSS 12272 kB`（模型 + NPU 上下文）。

### 3.2 控制延迟（关键：用户要求延迟优先）

各 160 次采样，同一套测量脚本：

| 条件 | p50 | p90 | p99 |
|---|---|---|---|
| 无 NPU（基线） | **26.1 ms** | 37.5 ms | 59.4 ms |
| NPU @ 10fps（默认） | **37.8 ms** | 68.5 ms | 96.3 ms |
| **NPU @ 2fps（采用）** | **29.2 ms** | 46.8 ms | 70.9 ms |
| 重启后复测 @2fps | 34.6 ms | 60.0 ms | 86.6 ms |

**结论：默认的 `npu_fps = 10` 会让 p50 涨 12ms（+45%），必须降下来。**
降到 **2fps** 后 p50 只比基线高 ~3ms，检测还够用（人和狗不会一秒内消失）。

> `npu_fps` 是最有效的旋钮。如果以后还想更快，可以再往下降（1fps），
> 代价是框更新更迟钝。

### 3.3 持久化（重启验证）

```
重启前: enable_npu=1  npu_fps=2  模型在 /usr/lib
重启后: enable_npu=1  npu_fps=2  模型仍在  rknpu Used=1  检测线程在跑
        RTSP 有流  控制 move/stop 正常
```

---

## 四、能力与限制

### 支持的目标（PFP 模型）

rkipc 用的是 **PFP = Person / Face / Pet**（人 / 脸 / 宠物），
`rockiva_model_type = small`（可选 medium / big，更准但更耗）。

**"狗"属于 Pet 类**，所以你要的"人和狗"覆盖到了。

### 默认只框"人"

社区源码分析（[rkipc 的 npu(iva) 学习笔记](https://www.cnblogs.com/tlnshuju/p/19092860)）指出：
rkipc 的 `rkipc_get_nn_update_osd()` **默认只画 person**，宠物框要改代码加分支：

```c
// rkipc/src/rv1106_ipc/video/video.c
if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PERSON) { draw_rect_2bpp(...); }
else if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PET) { draw_rect_2bpp(...); }  // 要自己加
```

**要框狗需要交叉编译 rkipc**。SDK 和工具链都在 WSL 里（`/root/sdk/luckfox-pico-main`），
这条路是通的，但属于下一步。

### 坐标是归一化的

rockiva 的坐标是 **1~10000 归一化值**，要乘以图像分辨率才是像素位置。
`objId` 是目标序号；目标消失再出现会 +1。

---

## 五、踩过的坑（都在下面文档里）

1. **`rknn_query` 通过 ctypes 必段错误** —— 试过按头文件修正 cmd 枚举、结构体大小、
   各种调用约定，仍 SIGSEGV。**改用交叉编译的 C 程序后一次通过。**
   板上没有编译器，但 WSL 里有 SDK 工具链。
   （另：`rknn_context` 在 ARM 上是 `uint32_t` 而不是 `uint64_t`，ctypes 用错了会踩。）

2. **`/userdata/rkipc.ini` 每次开机被模板覆盖** —— `RkLunch.sh` 第 122 行
   `cp $default_rkipc_ini $rkipc_ini -f`。**只改 ini 是白改，必须同时改
   `/oem/usr/share/rkipc-300w.ini`。**

3. **重启 rkipc 必须带 `LD_LIBRARY_PATH`**，否则 `can't load library 'librockit.so'`
   秒退，而它是开机链的一部分 → 看门狗复位板子。用
   `board/app/video_ctl.py` 的 `_restart_rkipc()`（见 `docs/VIDEO_RESTART.md`）。

4. **cycle snapshot 会写满 `/userdata`** —— 它只有 2.2MB，1920×1080 JPEG 每张 4KB，
   每秒一张，**20 秒就 100% 满**。要用的话必须改 `mount_path` 到 SD 卡。

5. **板上没有任何 JPEG 解码器** —— PIL 编译时没带 jpeg（`features.check('jpg')=False`），
   也没有 ffmpeg/libjpeg/djpeg。所以"抓快照喂 NPU"这条路走不通
   （但最后用不上，因为 rkipc 自己就能检测）。

---

## 六、复现步骤

```sh
# 1) 放模型 (从 SDK 取, 或从本仓库 tools/npu/models/ 取)
cp object_detection_pfp.data           /usr/lib/
cp object_detection_pfp_896x512.data   /usr/lib/

# 2) 开 NPU (ini 和模板都要改!)
sed -i 's/^enable_npu.*/enable_npu = 1/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
sed -i 's/^npu_fps.*/npu_fps = 2/'       /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini

# 3) 用正确方式重启 (别手搓, 会挂图传+触发看门狗)
python3 -c "
import sys; sys.path.insert(0,'/userdata/car'); import video_ctl
print(video_ctl._restart_rkipc())"

# 4) 验证
lsmod | grep rknpu                        # Used=1
grep ROCKIVA /tmp/rkipc.log               # ROCKIVA_BA_Init success
grep -c 'model data not found' /tmp/rkipc.log   # 0
python3 tools/npu/measure-control-latency.py NPU   # 看控制延迟
```

---

## 七、下一步（可选）

| 目标 | 做法 |
|---|---|
| **把"狗"也框出来** | 交叉编译 rkipc，在 `rkipc_get_nn_update_osd()` 加 PET 分支 |
| 提高检测精度 | `rockiva_model_type = medium` 或 `big`（更耗，需重测延迟）|
| 只在有人/狗时提高帧率 | 目前固定 2fps；可做成自适应 |
| 检测结果上报网页 | rkipc 把结果放共享结构，可加个 HTTP 接口读出来 |

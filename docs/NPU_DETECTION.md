# NPU 人/狗检测：已上线（实测报告）

> **状态：可用，已持久化（重启自动恢复）**
>
> 这块板子的 NPU 现在真的在跑 **人 / 脸 / 宠物（含狗）** 检测，
> 检测框由 rkipc 自己画到视频流上 —— **不需要另起进程抢摄像头，不需要写解码器**。

---

## 〇、要做的三件事（总览）

| # | 做什么 | 为什么 |
|---|---|---|
| 1 | 把 `object_detection_pfp.data` 放进 `/usr/lib/` | 固件里**没有**这个模型，rkipc 找不到就报错 |
| 2 | 打开 `enable_npu = 1` + `npu_fps = 15` | 开启检测；`npu_fps` 是送帧节奏，实测 15fps 只多 ~2ms 延迟 |
| 3 | **重编译 rkipc，给宠物加画框分支** | 原厂只画"人"，狗检测到了也不画框 |

第 3 步是可选的（不做也能检测，只是狗不出框）。

---

## 一、最终配置

| 项 | 值 | 说明 |
|---|---|---|
| `/usr/lib/object_detection_pfp.data` | 1.0 MB | **关键**：rockiva 检测模型，固件原本没有 |
| `/usr/lib/object_detection_pfp_896x512.data` | 1.0 MB | 备用分辨率 |
| `/oem/usr/bin/rkipc` | 461648 B | 自编译版，带 PET 画框分支 |
| `enable_npu = 1`<br>`npu_fps = 15` | ini **和**模板都要改 | 实测 15fps 是"检测流畅度 vs 控制延迟"的平衡点 |

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

拷到板上 `/usr/lib/` 即可。**板子 rootfs 有 110MB 余量，两个文件占 2MB。**

> **注意**：rockiva 用的是 RockX `.data` 格式，**不是** `.rknn`。
> 网上能找到的 `yolov5.rknn` **不能**替代它。

---

## 三、让狗也出框（重编译 rkipc）

### 3.1 原厂只画"人"

`rkipc/src/rv1106_ipc/video/video.c` 的 `rkipc_get_nn_update_osd()` 里，
画框只有 4 个分支：

```c
if      (type == ROCKIVA_OBJECT_TYPE_PERSON)       draw_rect_2bpp(..., INDEX_0);  // 蓝
else if (type == ROCKIVA_OBJECT_TYPE_FACE)         draw_rect_2bpp(..., INDEX_0);  // 蓝
else if (type == ROCKIVA_OBJECT_TYPE_VEHICLE)      draw_rect_2bpp(..., INDEX_1);  // 红
else if (type == ROCKIVA_OBJECT_TYPE_NON_VEHICLE)  draw_rect_2bpp(..., INDEX_1);  // 红
// ✗ 没有 PET —— 狗被检测到但不画框
```

`rockiva_common.h` 里 `ROCKIVA_OBJECT_TYPE_PET = 6 /* 宠物(猫/狗) */`。

### 3.2 加一个分支

```c
} else if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PET) {
    draw_rect_2bpp((RK_U8 *)stCanvasInfo.u64VirAddr, stCanvasInfo.u32VirWidth,
                   stCanvasInfo.u32VirHeight, x, y, w, h, line_pixel,
                   RGN_COLOR_LUT_INDEX_1);   // 红框
}
```

> ⚠️ **只有 `INDEX_0`(蓝) 和 `INDEX_1`(红) 两个颜色**，没有 `INDEX_2`。
> 写 `INDEX_2` 会编译失败。宠物用 `INDEX_1`，和 VEHICLE 一样（原厂既有做法）。

### 3.3 编译（WSL 里，SDK 已配好）

```bash
SDK=/root/sdk/luckfox-pico-main
export PATH=$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH
cd $SDK/project/app/rkipc/build
make rkipc -j4
# 产物: build/src/rv1106_ipc/rkipc
```

**构建目录是现成的**（`build/CMakeCache.txt` 已指向 uclibc 工具链），
所以是增量编译，很快。

**必须 strip**，否则体积对不上（644KB vs 板子 461KB）：

```bash
arm-rockchip830-linux-uclibcgnueabihf-strip rkipc_pet
# 461648 字节 —— 与板子原版一致
```

`sdktools` 见 `tools/npu/build-rkipc-pet.sh`。

---

## 四、实测数据

### 4.1 检测确实在跑

```
rknpu Used = 1                          <- NPU 被 rkipc 持有
fd -> /dev/rknpu
[检测线程] iva_main_loop                <- rockiva 主推理循环
[检测线程] RkipcGetIVS                  <- IVS 结果消费
[检测线程] RkipcNpuOsd                  <- 把框画到 OSD
[rockiva.c] ROCKIVA_BA_Init success
'model data not found' 次数 = 0
```

### 4.2 控制延迟与 npu_fps（关键调优）

**`npu_fps` 控制"每秒送几帧给 NPU"**（源码 `video.c:543`）：

```c
int npu_cycle_time_ms = 1000 / rk_param_get_int("video.source:npu_fps", 10);
// 每轮: RK_MPI_VI_GetChnFrame(VIDEO_PIPE_2) -> rockiva_write_nv12_frame_by_phy_addr()
//       -> 释放帧 -> usleep(补足周期)
```

送的是 **`video.2` 通道（960×540）**，不是主码流 —— 这也是它便宜的原因。

#### 干净对比（每档等 60 秒稳定，各 3 轮 × 160 次采样）

| npu_fps | p50（3 轮） | p90 | 说明 |
|---|---|---|---|
| **2** | 25.6 / 26.0 / 26.0 ms | ~37 ms | 最省，但框更新慢 |
| **10** | 27.5 / 27.0 / 27.4 ms | ~40 ms | 原厂默认值 |
| **15**（采用） | — | — | 实测 p50 28.8ms |
| **30** | 30.1 / 29.8 / 30.0 ms | ~48 ms | 上限，再高没意义 |

**结论：提速代价很小。** 从 2 → 30fps（15 倍）只多 4ms。
**`npu_fps = 15` 是平衡点**：检测比 2fps 快 7 倍，延迟代价约 2-3ms。

> ⚠️ **测量方法很重要**：我第一次扫描时每档只等 18 秒就测，得到
> "fps=10 → 34ms"的结论，**是错的** —— rkipc 重启后 ISP/NPU 还在初始化。
> 等 60 秒后复测，fps=10 其实只有 27ms。**改了配置必须等系统稳定再测。**

> 注意：`npu_fps` 不是"检测帧率上限"，而是"送帧节奏"。
> 实际上限还受 `VIDEO_PIPE_2` 的出帧率约束。

### 4.3 持久化（冷启动验证）

```
重启前: rkipc md5 c13eff70...  enable_npu=1  npu_fps=15  模型在 /usr/lib
重启后: rkipc md5 c13eff70...  enable_npu=1  npu_fps=15  模型仍在
        rknpu Used=1  检测线程在跑  RTSP 有流  控制正常
```

三处都在持久分区：

* `/oem/usr/bin/rkipc` → oem 分区（ubi4）
* `/usr/lib/*.data` → rootfs（ubi0）
* 配置 → ini + `/oem/usr/share/rkipc-300w.ini` 模板（开机 `RkLunch.sh` 会拷模板覆盖 ini）

---

## 五、能力与限制

### 支持的目标

rockiva 用的 **PFP = Person / Face / Pet**，`rockiva_model_type = small`
（可选 medium / big，更准但更耗）。**"狗"属于 Pet 类** —— 加上 PET 画框分支后
人和狗都会出框。

### 画框颜色

| 目标 | 颜色 |
|---|---|
| 人 / 脸 | 蓝 (`INDEX_0`) |
| 车 / 非机动车 / **宠物** | 红 (`INDEX_1`) |

只有两种颜色可用。

### 坐标是归一化的

rockiva 的坐标是 **1~10000 归一化值**，代码里乘以 `video_width/height` 得到像素。

---

## 六、踩过的坑

1. **`rknn_query` 通过 ctypes 必段错误** —— 试过修正 cmd 枚举、结构体大小、
   各种调用约定，全部 SIGSEGV。**改用交叉编译的 C 程序后一次通过。**
   另：`rknn_context` 在 ARM 上是 `uint32_t` 不是 `uint64_t`。

2. **`RGN_COLOR_LUT_INDEX_2` 不存在** —— 只有 0/1 两个色。写 2 编译报错。

3. **改 if-else 链时锚点别带上收尾的 `}`** —— 我第一次插入时把 if 链
   提前闭合了，变成 `} } else if (...)`，语法错误。

4. **`/userdata/rkipc.ini` 每次开机被模板覆盖** —— `RkLunch.sh` 第 122 行
   `cp $default_rkipc_ini $rkipc_ini -f`。**必须同时改模板。**

5. **重启 rkipc 必须带 `LD_LIBRARY_PATH`**，否则 `can't load library 'librockit.so'`
   秒退，而它是开机链的一部分 → 看门狗复位板子。用
   `board/app/video_ctl.py` 的 `_restart_rkipc()`（见 `docs/VIDEO_RESTART.md`）。

6. **cycle snapshot 会写满 `/userdata`** —— 它只有 2.2MB，1920×1080 JPEG 每秒一张，
   **20 秒就 100% 满**。用的话要把 `mount_path` 指到 SD 卡。

7. **板上没有任何 JPEG 解码器** —— PIL 编译时没带 jpeg（`features.check('jpg')=False`），
   也没有 ffmpeg/libjpeg。所以"抓快照喂 NPU"这条路走不通（最后也没用上）。

---

## 七、复现步骤

```sh
# 1) 放模型
cp object_detection_pfp.data           /usr/lib/
cp object_detection_pfp_896x512.data   /usr/lib/

# 2) 开 NPU (ini 和模板都要改!)
sed -i 's/^enable_npu.*/enable_npu = 1/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
sed -i 's/^npu_fps.*/npu_fps = 15/'       /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini

# 3) (可选) 换带 PET 分支的 rkipc
cp rkipc_pet /oem/usr/bin/rkipc && chmod +x /oem/usr/bin/rkipc

# 4) 用正确方式重启 (别手搓!)
python3 -c "
import sys; sys.path.insert(0,'/userdata/car'); import video_ctl
print(video_ctl._restart_rkipc())"

# 5) 验证
lsmod | grep rknpu                        # Used=1
grep ROCKIVA /tmp/rkipc.log               # ROCKIVA_BA_Init success
grep -c 'model data not found' /tmp/rkipc.log   # 0
python3 tools/npu/measure-control-latency.py NPU   # 控制延迟
```

### 回滚

```sh
# rkipc 回滚到原厂版 (备份在 SD 卡上)
cp /mnt/sdcard/npu/rkipc_stock_backup /oem/usr/bin/rkipc
# 关掉 NPU
sed -i 's/^enable_npu.*/enable_npu = 0/' /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini
```

---

## 八、下一步（可选）

| 目标 | 做法 |
|---|---|
| 提高检测精度 | `rockiva_model_type = medium` / `big`（更耗，需重测延迟）|
| 宠物用不同颜色 | 需要给 `draw_rect_2bpp` 加第三个 LUT 色 |
| 检测结果上报网页 | rkipc 的结果在共享结构里，可加 HTTP 接口读出来 |
| 有人/狗时自适应提帧率 | 目前固定 2fps |


---

## 一、最终配置（就这么三件事）

| 项 | 值 | 说明 |
|---|---|---|
| `/usr/lib/object_detection_pfp.data` | 1.0 MB | **关键**：rockiva 的检测模型，固件里原本没有 |
| `/usr/lib/object_detection_pfp_896x512.data` | 1.0 MB | 同上（备用分辨率）|
| `/userdata/rkipc.ini` + `/oem/usr/share/rkipc-300w.ini` | `enable_npu = 1`<br>`npu_fps = 15` | 开启检测；帧率降到 2 保控制延迟 |

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
sed -i 's/^npu_fps.*/npu_fps = 15/'       /userdata/rkipc.ini /oem/usr/share/rkipc-300w.ini

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

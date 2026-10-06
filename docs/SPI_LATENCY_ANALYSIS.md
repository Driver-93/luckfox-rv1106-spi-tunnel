# SPI 隧道延迟瓶颈：实测分析

**日期**: 2026-10-04
**目标**: 解决高分辨率（1296p / 6 Mbps）下视频延迟高的问题
**约束**: 不允许通过降低视频画质解决（用户明确要求）
**方向**: 内核模块 + DMA

---

## 一、结论先行

**瓶颈是每帧固定开销，不是带宽，也不是 SPI 时钟频率。**

实测（板子 `/dev/spidev0.0`，20 MHz，4096 字节帧）：

```
单帧耗时        3.10 ms
纯线上时间      1.64 ms   (4096 B × 8 / 20 MHz)
────────────  ────────
每帧净开销      1.46 ms   (47%)
```

**吞吐饱和在 ~10.5 Mbps**（320 帧/秒 × 4096 B）。1296p 需要 6 Mbps，
看似够用，但：

* 6 Mbps 已占天花板 **57%**，余量不足
* 链路是 **stop-and-wait**（一帧一个来回），RTT 决定一切
* 饱和时 RTT 涨到 **17.8 ms** → 吞吐上限掉到 **1.82 Mbps**

---

## 二、关键测量数据

### 2.1 单帧成本 vs 数据量（N=1，逐字节分离）

| 帧大小 | 实测耗时 | 纯线上时间 | 开销 |
|---|---|---|---|
| 16 B | 0.719 ms | 0.006 ms | **0.713 ms** |
| 256 B | 1.027 ms | 0.102 ms | 0.925 ms |
| 1024 B | 1.345 ms | 0.410 ms | 0.935 ms |
| 2048 B | 1.894 ms | 0.819 ms | 1.075 ms |
| 3072 B | 2.396 ms | 1.229 ms | 1.167 ms |
| **4096 B** | **2.860 ms** | **1.638 ms** | **1.222 ms** |

**推导**：固定开销 ≈ **0.71 ms/次传输**（16 B 时几乎全是开销），
边际带宽 = (2.860−0.719)/(4096−16) = **0.53 µs/字节 ≈ 15.1 Mbps**。

### 2.2 持续吞吐（100 帧背靠背）

| 帧大小 | frames/s | 吞吐 |
|---|---|---|
| 16 B | 1145 | 0.15 Mbps |
| 512 B | 801 | 3.28 Mbps |
| 1024 B | 654 | 5.36 Mbps |
| 2048 B | 489 | 8.01 Mbps |
| **4096 B** | **320** | **10.47 Mbps** |

**吞吐随帧大小增长而饱和** —— 证明是"每次传输的固定成本"在限制，
而不是位速率。

### 2.3 为什么 stop-and-wait 是致命的

吞吐 = 每帧字节数 ÷ RTT：

| RTT | 吞吐上限 |
|---|---|
| 3.84 ms（单帧 p50） | 8.45 Mbps |
| 8 ms | 4.06 Mbps |
| **17.8 ms（饱和实测）** | **1.82 Mbps** |

**1296p 要 6 Mbps，要求 RTT ≤ 5.41 ms。** 饱和时 17.8 ms，
差 3 倍以上。

---

## 三、已排除的方案（都实测过，不能走）

### ❌ 3.1 链式多帧 `SPI_IOC_MESSAGE(N)`

```
N=2, size=2048, TOTAL=4096 -> OK 2.89 ms
N=1, size=4096, TOTAL=4096 -> OK 4.87 ms
N=4, size=1024, TOTAL=4096 -> OK 7.36 ms
N=2, size=4096, TOTAL=8192 -> FAIL [Errno 90] Message too long
```

**两个结论**：

1. `bufsiz=4096` 是硬上限（`/sys/module/spidev/parameters/bufsiz`，
   只读，运行时不能改）。总字节超过 4096 直接 `EMSGSIZE`。
2. **链式消息是串行的，不是并行的** —— N=4 比 N=1 还慢（7.36 vs 4.87 ms
   同样总字节）。所以链式 ioctl **无法实现流水线**。

### ❌ 3.2 变长传输（上一轮已试过，失败并回退）

固定 4096 字节里的空心跳只用了 16 字节，但**改为变长不会提高吞吐上限**，
因为瓶颈是"往返次数"，不是"每帧长度"（每帧已经塞了 3 个包 = 4056 B）。

而且上次变长尝试因为握手设计错误（`reserved` 字段无法同时表达
两个方向的需求）把整条隧道搞挂了。**不要再试。**

### ❌ 3.3 提高 SPI 时钟

20 → 24 MHz 只省 0.27 ms/帧的线上时间，而每帧开销是 1.46 ms。
**收益 < 20%，且 24 MHz 已被实测证明不稳定。**

### ❌ 3.4 降码率

用户明确拒绝。且这只是绕过 RTT 限制，没有修问题。

---

## 四、为什么内核模块是对的解法

内核模块正好消掉那 **1.46 ms/帧** 的固定开销，因为开销来源全部是
用户态 ↔ 内核态的成本：

| 开销来源 | 内核模块如何消除 |
|---|---|
| `ioctl` 系统调用 | 不需要 —— 直接调 SPI 子系统 |
| 用户态↔内核态数据拷贝 ×2 | 不需要 —— 内核内直接 DMA |
| Python 解释器 + 对象分配 | 不需要 —— 纯 C |
| 等待唤醒/调度延迟 | 中断直接处理，可流水线 |
| 每帧一次完整的"提交-等待" | **可提交 N 帧不等待 → 真正的流水线** |

**预期收益**：

* 消除 ~1.46 ms/帧开销 → 单帧从 3.10 ms 降到 ~1.7 ms
* 可流水线（内核里能同时挂多个 transfer）→ 吞吐不再受 RTT 限制
* CPU 从 24–26% 降到几个百分点，把单核让给 rkipc 编码

这两项叠加，6 Mbps 不再吃紧，**视频延迟随之下降**（队列不积压）。

---

## 五、构建内核模块的前置条件（当前全部缺失）

| 条件 | 状态 |
|---|---|
| 内核模块支持 | ✅ 有（`tun.ko` 正在运行，`/oem/usr/ko/`） |
| 精确匹配的内核树 / `Module.symvers` | ❌ **没有** |
| ARM 交叉编译器 | ❌ **没有** |
| Linux 构建环境 | ⏳ WSL 正在装（无发行版） |
| Luckfox SDK | ❌ **全盘搜索无结果** |

**必须是**：
```
Linux 5.10.160 #1 Fri Sep 11 22:06:09 CST 2026 armv7l
编译器: arm-rockchip830-linux-uclibcgnueabihf-gcc 8.3.0
        (crosstool-NG 1.24.0)
```

`.ko` 的 vermagic 必须逐字节匹配，且依赖内核内部符号表。
**版本不匹配会 insmod 失败，或更糟：加载成功但内存布局错误 → 内核 panic
→ 只能物理断电。**（刷 C5 那次已经体验过一次）

---

## 六、内核模块可行性核查（2026-10-04 实测）

### 6.1 ✅ 好消息：CONFIG_MODVERSIONS 是关的

```
__crc_ symbol count in /proc/kallsyms : 0
=> MODVERSIONS OFF —— 不做逐符号 CRC 校验
```

**这意味着不需要精确的 `Module.symvers`**，只需要 vermagic 匹配：

```
vermagic=5.10.160 mod_unload ARMv7 thumb2 p2v8
```

（从 `/oem/usr/ko/tun.ko` 读出，这是必须逐字节匹配的字符串）

模块签名校验也没开（dmesg 无相关输出）。

### 6.2 ✅ 好消息（2026-10-04 修正）：SDK 的 `main` 分支**就是** 5.10.160

**我之前的判断是错的。** 我只看了一眼分支列表里的 `5.10.110`，
就断定"SDK 没有 5.10.160"。实际上 `5.10.110` 只是**一个旧分支**，
**默认分支 `main` 就是 5.10.160**。

直接从 Gitee API 读 `sysdrv/source/kernel/Makefile` 实测：

| 分支 | VERSION | PATCHLEVEL | SUBLEVEL |
|---|---|---|---|
| **`main`（默认）** | 5 | 10 | **160** ✅ |
| `5.10.110` | 5 | 10 | 110 |

**与板子的 `5.10.160` 完全一致。**

完整分支列表（GitHub 与 Gitee 一致）：
```
5.10.110, bk-2609, busybox, main, prebuilt-rootfs
```

### 6.2.1 ✅ defconfig 也符合

SDK 提供 `luckfox_rv1106_linux_defconfig`（正是这块板子的配置），实测：

```
CONFIG_MODULES=y            <- 能编模块
CONFIG_MODULE_UNLOAD=y      <- vermagic 里的 mod_unload 对上了
CONFIG_SPI=y
CONFIG_SPI_SPIDEV=y         <- /dev/spidev0.0 的来源
CONFIG_NETDEVICES=y

MODVERSIONS     未提及 -> 默认关闭  ✅ 与板子一致（__crc_ = 0）
MODULE_SIG      未提及 -> 默认关闭  ✅ 与板子一致（无签名校验）
CONFIG_LOCALVERSION_AUTO is not set -> vermagic 里没有额外后缀 ✅
```

**三项关键条件全部吻合**，vermagic 匹配的可能性很高。

### 6.2.2 仍需注意的风险

* 板子 `VERSION=-ge2b0ffa22-dirty` 里的 **`-dirty`** 说明当时源码树
  有本地改动。但既然 `LOCALVERSION_AUTO` 是关的，**`-dirty` 不会进入
  vermagic** —— vermagic 只由 `CONFIG_MODULE_UNLOAD`、架构、thumb2、
  p2v8 这些决定，都与 defconfig 一致。所以风险比我原先估计的小很多。
* 固件包 `Luckfox_Pico_Pro_Max_Flash_250607` 的文件日期（2025-06-26）
  与内核编译日期（2026-09-11）不符 —— 这个矛盾仍未解释，
  但**不影响模块兼容性判断**，因为看的是内核源码版本而非固件包日期。

### 6.3 ✅ 已解决：Windows 上无法 checkout 内核源码

```
error: invalid path 'sysdrv/source/kernel/drivers/gpu/drm/nouveau/nvkm/subdev/i2c/aux.c'
error: invalid path 'sysdrv/source/kernel/include/soc/arc/aux.h'
```

Linux 内核源码含 `aux.c` / `aux.h`，而 **`aux` 是 Windows 保留设备名**。
**这个问题在 WSL 里不存在**（Linux 文件系统没有保留名限制）。

### 6.4 ✅ WSL 环境已就绪（2026-10-04 完成）

Store 安装方式在本机**完全不可用**（三次失败：卡 `Installing`、
`ERROR_ALREADY_EXISTS`、下载 0 MB）。最终用**直接下载 + 本地导入**解决：

```powershell
# Ubuntu 的 WSL 镜像已从 cloud-images.ubuntu.com 迁到 cdimages.ubuntu.com
# 旧 URL 全部 404，这是踩过的坑
$url = 'https://cdimages.ubuntu.com/ubuntu-wsl/noble/daily-live/current/noble-wsl-amd64.wsl'
Invoke-WebRequest -Uri $url -OutFile noble-wsl-amd64.wsl   # 371 MB, 下了 73 分钟
wsl --install --from-file noble-wsl-amd64.wsl --name Ubuntu --no-launch
```

结果：

```
Ubuntu 24.04.5 LTS, root, 24 CPU, 955 GB free, git/make/gcc 可用
网络正常 (apt-get update 成功)
```

### 6.5 ✅ 网络问题已解决：用 Windows 代理（比直连快 10 倍）

**实测对比**（同一个文件，同一次测量，60 秒上限）：

| 方式 | 速度 | 60 秒下载量 |
|---|---|---|
| 直连 | 228 KB/s | 13.7 MB |
| **代理 `127.0.0.1:10809`** | **2,426 KB/s** | **145.5 MB** |

**代理快 10.6 倍。** 而且直连是"先快后塌"（短测 68 KB/s，
长测平均 228 KB/s，说明中途会停），代理则稳定跑满。

**连通性差异**：

| 主机 | 直连 | 代理 |
|---|---|---|
| `gitee.com` | ✅ 200（589 KB/s） | — |
| `github.com` | ✅ 慢 | ✅ |
| `raw.githubusercontent.com` | ❌ **失败** | ✅ **200** |
| `codeload.github.com` | ✅ 200 | ✅ 快 |
| `www.google.com` | ❌ | ✅ 200 |

**注意**：`raw.githubusercontent.com` 在 WSL 里解析到 **IPv6** 地址，
这可能是直连失败的原因（`curl -4` 也没救回来）。

**关键坑**：Windows 代理只监听 `127.0.0.1`，**WSL 访问不到**
（WSL 看到的 Windows 主机是 `172.19.96.1`，那些端口从 WSL 侧全是 closed）。
所以在 **WSL 里不能用这个代理**，需要在 **Windows 侧**下载，再拷进 WSL。

**可用的下载方式**：
```powershell
curl.exe -L -x http://127.0.0.1:10809 -o sdk.tar.gz -C - \
  --retry 10 --retry-delay 5 --retry-all-errors \
  "https://codeload.github.com/LuckfoxTECH/luckfox-pico/tar.gz/refs/heads/main"
```

`-C -` 支持断点续传，配合 `--retry-all-errors` 能扛住网络抖动。

---

## 八、⚠️ insmod 实验：vermagic 匹配但**内核被搞坏了**（2026-10-04）

### 8.1 ✅ 已验证成功：工具链完全正确

经过完整验证，**编译环境已经打通**：

| 条件 | 结果 |
|---|---|
| SDK 内核源码 | ✅ `5.10.160`（`main` 分支，1060 MB 下载完成）|
| 交叉编译器 | ✅ **`arm-rockchip830-linux-uclibcgnueabihf-gcc (crosstool-NG 1.24.0) 8.3.0`** |
| | **与板子编译时用的是同一个**（字符串完全一致）|
| defconfig | ✅ `luckfox_rv1106_linux_defconfig` |
| 编译出 `.ko` | ✅ 77528 字节 |
| **vermagic** | ✅ **`5.10.160 mod_unload ARMv7 thumb2 p2v8` 逐字节匹配** |

工具链位置：`tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/`
（SDK 自带，无需外部下载）

### 8.2 ❌ 但 insmod 段错误，并把内核搞坏了

```
insmod /tmp/hello.ko
Segmentation fault
insmod exit=139
```

**后果（比预期严重）**：

```
/proc/modules        -> 永久挂起，读一次卡死一次
lsmod                -> 依赖 /proc/modules，同样挂起
受影响进程           -> 11 个卡在 D 状态（不可中断睡眠）
load average         -> 从正常值涨到 22
之后所有 SSH 命令     -> 频繁超时（因为 load 太高 + 卡住的进程堆积）
```

**`kill -9` 杀不掉那些进程** —— 它们卡在不可中断的内核睡眠里。
**每次我执行 `grep hello /proc/modules` 都又卡死一个进程**，越查越糟。

### 8.3 板子当前状态：功能正常，但模块子系统坏了

| 功能 | 状态 |
|---|---|
| SPI 隧道 | ✅ 0% 丢包，13.5 ms RTT |
| spinet | ✅ 运行中 |
| rkipc | ✅ 运行中 |
| mediamtx | ✅ 运行中 |
| 网页 `/api/status` | ✅ 正常返回 |
| RTSP 554 | ✅ `200 OK` |
| **`/proc/modules`** | ❌ **永久挂起** |
| **`lsmod`** | ❌ **挂起** |
| **load average** | ❌ **22（正常应 <2）** |

**内核没有 panic，板子没重启**（uptime 连续），图传和控制都还能用。
**但模块子系统已损坏** —— 无法查看模块列表，且残留进程拖高负载。

**恢复方法：断电重上电**（用户已重启过一次，但 load 仍高 —— 说明残留进程
在新会话里又被我的命令重新卡住了）。

### 8.4 根本原因：没有真实的 `Module.symvers`

构建日志早就警告了，**我没重视**：

```
WARNING: Symbol version dump "Module.symvers" is missing.
         Modules may not have dependencies or modversions.
WARNING: modpost: Symbol info of vmlinux is missing.
         Unresolved symbol check will be entirely skipped.
```

`modules_prepare` **不生成 `Module.symvers`** —— 那需要**完整编译内核**
（`make vmlinux` / `modules`）。没有符号表，模块里的未解析符号
会被"跳过检查"，加载时内核试图解析这些符号 → 崩溃。

**vermagic 匹配只是第一道门。符号完整性是第二道，我跳过了。**

### 8.5 我的错误（必须记住）

1. **拿到 vermagic 匹配就以为成功了**，直接 insmod，跳过了符号验证。
2. **没有先做只读检查**（`modinfo`、符号比对），而是把第一个编出来的
   模块直接塞进内核。
3. **故障后没意识到 `grep /proc/modules` 会继续卡死进程** —— 我反复执行
   同一个命令，把 11 个进程堆进了 D 状态，让情况恶化。
   正确做法：一旦发现 `/proc/modules` 挂起，**立刻停止读它**。
4. **测试前没有准备好恢复手段**（比如先确认断电方便）。

### 8.6 ✅ 已解决：完整编译内核，生成真实 `Module.symvers`

```bash
cd /root/sdk/luckfox-pico-main/sysdrv/source/kernel
export ARCH=arm
export CROSS_COMPILE=arm-rockchip830-linux-uclibcgnueabihf-
export PATH=/root/sdk/luckfox-pico-main/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH

make -j24 zImage modules
```

**实测只用 2.5 分钟**（24 核），产出：

```
Module.symvers : 610452 字节, 10600 个符号   <- 之前缺的就是这个
vmlinux        : 216 MB
zImage         : ready
```

**重新编译模块后，警告消失**（不再有 "Symbol version dump missing"），
而且模块的未解析符号只有 3 个：

```
U __aeabi_unwind_cpp_pr0
U __aeabi_unwind_cpp_pr1
U printk
```

### 8.6.1 ✅ 加载前预检（全部通过）

**这是上次没做、这次必须做的步骤。**

| 检查项 | 结果 |
|---|---|
| vermagic | ✅ `5.10.160 mod_unload ARMv7 thumb2 p2v8` |
| ELF 类型 | ✅ `ELF32 ARM`, `Version5 EABI` |
| license | ✅ `GPL`（内核不 taint）|
| `__versions` 段 | ✅ 不存在（MODVERSIONS 关闭，符合预期）|
| 3 个符号在 vmlinux | ✅ 全部 FOUND |
| **3 个符号在板子 `/proc/kallsyms`** | ✅ **全部 count=1**（这是最关键的）|
| `/proc/kallsyms` 可读 | ✅ 43750 个符号 |

**关键方法**：不能只看 `Module.symvers` —— `printk` 就不在里面
（它通过 `EXPORT_SYMBOL` 导出，不列在 symvers 中），但在 `vmlinux`
和板子的 `/proc/kallsyms` 里都存在。
**必须直接对着板子的 `/proc/kallsyms` 验证。**

### 8.7 ⚠️ 当前阻塞：板子的模块子系统仍处于损坏状态

上次失败的 insmod 留下**永久性损坏**，需要断电才能清除：

```
/proc/modules   -> 挂起
lsmod           -> 挂起
load average    -> 22.44（正常应 <2）
残留进程        -> 11 个卡在 D 状态，kill -9 无效
```

板子的**功能全部正常**（隧道 0% 丢包、rkipc、mediamtx、网页、RTSP 200），
但模块子系统读不了，负载被拖高。

**在断电重启清除之前，绝不能再 insmod** —— 否则是在已经损坏的状态上叠加。

### 8.8 下次测试的安全流程

1. ✅ 完整编译内核，拿到真实 `Module.symvers`（已完成）
2. ✅ 对着板子 `/proc/kallsyms` 逐个验证模块需要的符号（已完成）
3. ⏳ **断电重启，恢复 `/proc/modules`**
4. 重启后先确认 `lsmod` 正常、load < 2
5. **先加载一个极简模块**（只 `pr_info`，不注册任何东西）验证端到端
6. 通过后才写真正的隧道模块
7. **一旦 `/proc/modules` 挂起，立刻停止一切涉及它的操作**

---

## 九、下一步

1. ~~装好 WSL~~ ✅
2. ~~确认 SDK 内核版本~~ ✅ `main = 5.10.160`
3. ~~下载 SDK~~ ✅ 1060 MB
4. ~~验证编译器~~ ✅ 与板子完全一致
5. ~~验证 vermagic~~ ✅ 逐字节匹配
6. ~~完整编译内核生成 `Module.symvers`~~ ✅ 10600 个符号
7. ~~加载前符号预检~~ ✅ 全部通过
8. ⏳ **断电重启板子** ← 现在卡在这里
9. 加载极简模块验证端到端
10. 编写隧道 `.ko`

### 关键判断点（先做这个，别急着写代码）

**在写任何模块代码之前，必须先验证**：用 SDK 编一个
"hello world" 空模块，看它的 `vermagic` 是否等于
`5.10.160 mod_unload ARMv7 thumb2 p2v8`。

* 匹配 → 可以继续
* 不匹配 → 内核路线在当前固件上不可行，及时止损

这一步只要几分钟，但能避免在错误的方向上浪费几天。
**上次变长传输的教训就是没先做这个级别的验证就动手。**

---

## 八、重要提醒

* **动手前先插上网线**。上次刷 C5 时隧道是唯一恢复通道，结果板子失联，
  只能物理断电。
* 内核模块 panic 比 C5 刷坏更严重 —— 用户态程序崩了还能 SSH 进去，
  内核崩了只能断电。
* 建议先在用户态把"多帧在途"的协议逻辑验证正确，再把它搬进内核。
  协议 bug 和内核 bug 混在一起会极难排查。

---

## 十、内核模块实战（2026-10-04）：从"编不出来"到"跑起来"

### 10.1 ✅ 突破：自编内核 + 自编模块可以加载

前两次 insmod 段错误的根因是**模块与运行内核的结构体布局不一致** ——
板子的内核是改过的（`-dirty`），不是公开 SDK 编出来的。

**解决办法（用户提出，完全正确）**：不试图匹配现有内核，而是
**从官方 SDK 完整编译一个自洽的固件刷进去**。这样内核与模块同源，天然匹配。

实测结果：

```
刷入自编固件后:
  lsmod | grep spitun   ->  spitun  3803  0
  ip link show spitun0  ->  POINTOPOINT,NOARP,UP,LOWER_UP  mtu 1350

dmesg:
  spitun: loading (frame=4096, hdr=16)
  spitun: netdev spitun0 registered
  spitun: spi driver registered
  spitun: loaded OK
  spitun: tunnel thread started
```

**前两次的段错误彻底消失。** 内核态隧道代码跑起来了。

### 10.2 编译固件的完整流程（踩坑记录）

```bash
cd /root/sdk/luckfox-pico-main
# 关键: lunch 和 all 必须在同一个 shell —— build.sh 启动会清空所有 RK_* 变量
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | paste -sd:)   # 去掉 Windows PATH!
export PATH=$PWD/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH
printf "4\n1\n0\n" | ./build.sh lunch    # 4=Pro Max, 1=SPI_NAND, 0=Buildroot
./build.sh all
```

**踩过的坑**：

| 问题 | 原因 | 解决 |
|---|---|---|
| 交互式选板 | `read` 等输入 | `printf "4\n1\n0\n"` |
| 找不到工具链 | 需装进 PATH | `source env_install_toolchain.sh` |
| `RK_*` 变量丢失 | 每次启动清空 | lunch 与 all 同一个 shell |
| Buildroot 拒绝编译 | **Windows 的 15 条 PATH 泄漏**（含空格）| 过滤掉 `/mnt/*` |
| 缺 host 工具 | — | `dtc unzip texinfo pkg-config gperf` 等 |
| 模块进不了固件 | 手工 cp 会被 `modules_install` 覆盖 | 照抄 `drv_ko/motor/` 的外部模块写法，加进 `M_DIRS` |
| 驱动不 probe | **缺 `of_match_table`** | 加 `compatible = "spitun"` 匹配表 |

**最有价值的一条**：`sysdrv/drv_ko/Makefile` 里的
`M_DIRS := rockit kmpp wifi motor` —— 想加自己的模块，就照
`motor/` 的样子建目录并加进 `M_DIRS`。这是 SDK 支持外部模块的正规入口。

### 10.3 ⚠️ 当前阻塞：新固件里 SPI0 被禁用

刷入新固件后发现**没有 `/dev/spidev*`**，且只有一条 SPI 总线：

```
/sys/class/spi_master/  ->  spi2          (rockchip,sfc — 板载 NAND)
/sys/bus/spi/devices/   ->  spi2.0        (spi-nand)
```

**spi2 是 NAND 存储，不是接 C5 的总线。** 板子烧录包里的分区表
（`spi-nand0:256K(env)...`）也印证了这点。

原因：官方 SDK 的设备树里 `spi0` 是**默认关闭**的：

```dts
&spi0 {
    status = "disabled";              /* 原厂不知道外接了 C5 */
    spidev@0 { spi-max-frequency = <50000000>; };
    fbtft@0  { spi-max-frequency = <50000000>; };
};
```

**已修复**（在 `rv1106g-luckfox-pico-pro-max.dts`）：

```dts
&spi0 {
    status = "okay";
    max-freq = <20000000>;
    spitun: spitun@0 {
        compatible = "spitun";
        reg = <0>;
        spi-max-frequency = <20000000>;
        status = "okay";
    };
};
```

注意**没有启用 spidev**：一个 `spi_device` 只能绑一个驱动，内核模块需要
`spi_sync` 而不是用户态 `/dev/spidev` 节点。20MHz 是实测上限
（C5 在 24MHz 丢帧、40MHz 直接挂），原厂的 50MHz 会直接搞死 C5。

### 10.4 🔬 C5 侧的实测状态（串口确认）

C5 固件在跑，但状态是：

```
frames=83968  err=83968   ← 完全相等: 每一帧都失败
ip tx=0 rx=0 drop=0
wifi=DOWN  rssi=0  ip=0.0.0.0
SCAN: 候选[0] 'HUAWEI-ER15NM_5G_Wi-Fi5' 不在空中
```

**两件事**：

1. **SPI 帧在动但全部校验失败**。`frames` 约 31 帧/秒在涨，说明板子侧
   有人在发起传输；但 `err == frames` 说明每一帧的魔数/校验和都不对。
   这与"spi0 未启用、总线悬空回读垃圾"完全吻合 —— **不是协议问题**。

2. **C5 连不上 WiFi**：目标 SSID `HUAWEI-ER15NM_5G_Wi-Fi5` **不在空中**。
   路由器可能换过 5G 信道（扫描结果显示其他 AP 在 ch 11/44），
   或者 AP 没开。这是独立问题，等隧道通了再处理。

`frames = err` 这个"完全相等"是很强的证据：如果协议不匹配（比如长度或
字节序错），错误率会很高但不会是 100% 且恰好等于帧数。100% 失败 + 总线
不存在 = 物理层没通，而不是软件不匹配。

### 10.6 ✅ 内核态 SPI 隧道打通（2026-10-04 最终）

经过三次刷机，内核态隧道终于工作：

```
spi0.0  modalias=spi:spitun  driver=spitun      <- 驱动绑定成功
spitun: SPI ready: bus=0 cs=0 speed=20000000 Hz <- probe 成功
spitun: tunnel thread started
spitun0: UP, mtu 1350
```

**C5 侧的错误率从 100% 降到 0.4%** —— 这是协议匹配的铁证：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `frames` | 83968 | 30720 |
| `err` | **83968（100%）** | **122（0.4%）** |
| 含义 | 每帧都失败 | 协议已匹配 |

### 10.6.1 三次刷机的问题链（每次都定位到具体原因）

**第一次**（启用 spi0）：
- 现象：`spitun` 模块加载、`spitun0` 建好，但隧道线程内核 oops
- 根因：**`spi0.0` 被 `spidev` 抢走绑定**，`spi_dev` 仍为 NULL，
  而 `spi_sync(NULL,...)` 直接解引用空指针
- 证据：
  ```
  /sys/bus/spi/devices/spi0.0 -> modalias=spi:spidev
  dmesg: LR is at spitun_exchange+0xad/0x128 [spitun]
         r0 : 00000000  <- spi_dev == NULL
  ```

**第二次**（删掉 spidev 节点）：
- 已修：板级 dtsi 里的 `spidev@0` 被移除
  （两个 dts 文件都往 `&spi0` 加子节点会**合并**，
  我以为替换掉了其实没有）
- 同时加固驱动：`if (!spi_dev) return -ENODEV;`
  以及隧道线程**等待设备绑定**而不是崩溃

**第三次**：全部通过。

### 10.6.2 沉淀下来的经验（最重要的一条）

**设备树里两个 `reg = <0>` 的子节点会打架，谁赢不一定。**

```
&spi0 {
    spidev@0 { compatible = "rockchip,spidev"; reg = <0>; };   <- 板级 dtsi
    spitun@0 { compatible = "spitun";          reg = <0>; };   <- 我们的 dts
};
```
两个文件都 include 进同一个 `&spi0`，**节点合并**，spidev 赢了绑定。
症状是"驱动注册成功但永远收不到 probe"，很难从模块侧看出来 ——
必须查 `/sys/bus/spi/devices/*/driver` 才知道谁真正绑上了。

**另一条**：模块加载与设备 probe 的**时序没有保证**。
`insmod_ko.sh` 在开机早期加载模块，而 `spi0.0` 的 probe 可能更晚。
所以驱动里**任何直接用 `spi_dev` 的路径都必须判空**。

### 10.7 ⚠️ 当前唯一阻塞：C5 连不上 WiFi（与隧道无关）

```
C5 扫描到 8 个 AP，但目标 SSID 不在其中：
  SCAN: [00] 'ABC'           rssi -55  ch 44
  SCAN: [01] 'CMCC-4Wx7-5G'  rssi -67  ch 36
  SCAN: [02] 'CU-0877_5G'    rssi -83  ch 48
  ...
  SCAN: 候选[0] 'HUAWEI-ER15NM_5G_Wi-Fi5' 不在空中    <- 找不到
  SCAN: 候选[1] 'HUAWEI-ER15NM_5G'         不在空中
  SCAN: 候选[2] 'ABC_Wi-Fi5'               不在空中
```

`wifi=DOWN  rssi=0  ip=0.0.0.0`，所以 `ip tx=0 rx=0` ——
隧道本身是通的（`err` 只有 0.4%），只是 C5 没有 IP 通路，
所以没有 IP 报文可以转发。

**这是独立的 WiFi 配置/环境问题**，不是 SPI 隧道问题：
- 路由器可能换了信道或关了 5G
- 或者 AP 名字变了

注意 C5 扫到的都是 `ch 36/44/48`（5G 低信道），而板子有线网
（`eth0`）是通的，说明路由器本身在工作。

### 10.8 下一步

1. 确认路由器当前 SSID / 信道（或直接在 C5 固件里换成扫得到的 AP）
2. C5 联网后，隧道应立刻开始转发 IP：`ip tx/rx` 开始增长
3. 恢复 `/userdata` 备份（网页、摄像头、GPS 等）——
   注意**隧道脚本 `spinet.py` 不再需要**，隧道现在在内核里
4. 但 `/etc/init.d/S22spinet` 等仍需改为配置内核接口（`ip addr add` +
   路由），而不是启动 Python 进程

# 两个备份：用户态版 与 内核态版

> 只保留两份，对应隧道的两个世代。
>
> | 备份 | 隧道实现 | 接口名 | 文件数 |
> |---|---|---|---|
> | `backup_20261004_userspace/` | **用户态** Python 进程 `spinet.py`（+`tun.ko` 提供 TUN 设备）| `tun0` | 16 |
> | `backup_20261005_kernel/` | **内核模块** `spitun.ko`（内核线程 `[spitun]`）| `spitun0` | 34 |
>
> ⚠️ 两份**接口名不同**（`tun0` ↔ `spitun0`）。恢复时必须整套恢复，
> **不要混用** —— 混用会留下"静默失效"的 bug（见文末实例）。

---

## 一、用户态版：`backup_20261004_userspace/`

这是**最后一个用户态版本**（2026-10-04 10:45 抓取），也是过渡版：
`tun.ko` 已加载（提供 `/dev/net/tun` 语义的接口），但**转发逻辑仍在用户态 Python**。

```
_etc_init.d_S21tun                    加载 tun.ko
_etc_init.d_S22spinet                 启动 python3 spinet.py  (核心)
_etc_init.d_S24spinet_wd              隧道看门狗
_etc_init.d_S14timezone               时区
_etc_ntp.conf
_userdata_car_spinet.py               用户态隧道主控 (51KB, 1500+ 行)
_userdata_car_web_server.py           控制网页服务
_userdata_car_index.html
_userdata_car_car_motor.py
_userdata_car_cam_ctl.py
_userdata_car_video_ctl.py
_userdata_car_gps_tz.py
_userdata_car_loglimit.sh
_userdata_rkipc.ini
_userdata_spi_speed
_usr_share_udhcpc_default.script      DHCP helper 守卫
```

**恢复要点**：
- `spinet.py` 必须**脱离 SSH 会话**启动（它就是远程通道本身）：
  `setsid nohup python3 -u /userdata/car/spinet.py >>/userdata/spinet.log 2>&1 &`
- 接口是 `tun0`，地址 `10.77.0.2/24`
- 依赖 `tun.ko`（备份里**没有**包含这个 .ko，需从板子 `/oem/usr/ko/` 另行保存）

> 已知代价（当年被替换的原因）：单核 A7 上用户态忙轮询吃掉 25~36% CPU，
> 实测 `spinet` 累计 CPU 是 `rkipc` 的 6.2 倍，帧率掉 60%。

---

## 二、内核态版（当前）：`backup_20261005_kernel/`

2026-10-05 抓取的**运行中快照**，含当天的全部修复
（帧级重传、发送队列 256、小包优先、策略路由、视频切换修复）。

```
etc_init.d/      S20lo S21wdt S22spinet S23web S24spinet_wd S25mediamtx
                 S99rtcinit S99luckfoxconfigload   (+ _LIST.txt 全部清单)
car/             web_server.py car_motor.py car_config.json index.html
                 video_ctl.py cam_ctl.py gps_tz.py loglimit.sh
oem_ko/          spitun.ko (当前) spitun_prev.ko spitun_new.ko spitun_old.ko
udhcpc/          default.script           (守卫: spitun0 不被 flush)
proc_info/       uname.txt lsmod.txt cmdline.txt ip_addr.txt ip_rule.txt
                 route_t100.txt tun_stats.txt md5.txt
rkipc.ini  rkipc-300w.ini.oem  mediamtx.yml  car_config.json
```

**恢复要点**：

1. **内核模块**：`cp oem_ko/spitun.ko /oem/usr/ko/spitun.ko`（开机由
   `/oem/usr/ko/insmod_ko.sh` 加载，该行是 `__insmod spitun.ko`）
2. **init 脚本**：拷回 `/etc/init.d/` 并 `chmod 755`
3. **应用**：拷回 `/userdata/car/`
4. **接口配置**（`S22spinet` 已包含）：
   ```sh
   ip addr add 10.77.0.2/24 dev spitun0
   ip link set spitun0 mtu 1350 up
   # 回程路由: 按源地址分流, 不写死客户端 IP
   ip rule add from 10.77.0.2 lookup 100 pref 100
   ip route replace 192.168.3.0/24 dev spitun0 src 10.77.0.2 table 100
   ```
5. **设备树/内核**：⚠️ **不在备份里**。`spitun` 驱动需要
   `&spi0 { status="okay"; spitun@0 { compatible="spitun"; }; }`，
   这就要求**自编译内核**。模块本身必须与内核同源编译（见下）。

### 重新编译 spitun.ko（WSL）

```bash
# SDK 在 /root/sdk/luckfox-pico-main (13GB, WSL Ubuntu)
export PATH=/root/sdk/luckfox-pico-main/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH
cd /root/spitun_build   # 里面有 Makefile (obj-m := spitun.o, KDIR=objs_kernel)
make
# 产物 spitun.ko ~362KB, vermagic=5.10.160
```
源码副本：仓库 `spitun_kmod/spitun.c`（与 SDK 的 `drv_sys/…/spitun/spitun.c` 保持同步）。

### 热替换（会断网几秒，必须后台跑）

```sh
cp -f /userdata/spitun_new.ko /oem/usr/ko/spitun.ko
setsid nohup sh /userdata/reload_spitun.sh >/userdata/reload_out.txt 2>&1 </dev/null &
# 脚本会 rmmod/insmod 并自动验证; 失败自动回滚到 /userdata/spitun_prev.ko
```

---

## 三、⚠️ 混用两份备份会出的问题（真实踩过）

**接口名从 `tun0` 改成 `spitun0`，但有些地方写死了旧名字**，于是出现"静默失效"：

| 出问题的位置 | 写死的旧名 | 后果 |
|---|---|---|
| `video_ctl.py` 杀 udhcpc | `"tun0" in cmd` | **一个都杀不掉 → 554 端口被 udhcpc 占用 → rkipc bind 失败 → 图传切换必失败、黑屏**（2026-10-05 修复）|
| `/userdata/car/spinet.py`（残留文件）| 通篇 `tun0` | 排查时被误导（该文件已不执行）|
| 文档 `ISSUES.md` / `REMOTE_ACCESS.md` | `S99z_spinet`、`tun0` | 按文档操作会找不到脚本 |

**结论：换世代就整套换，并全局搜一遍 `tun0`。**

---

## 四、恢复后的自检清单

```sh
lsmod | grep spitun                     # 模块在
ps | grep '[s]pitun'                    # 内核线程 [spitun] 在
cat /sys/class/net/spitun0/tun_stats    # 帧/重传/丢包计数
cat /sys/class/net/spitun0/c3_status    # C5 状态帧 (up_ms 在涨 = 隧道活)
ip rule | grep 10.77.0.2                # 策略路由在
ip route show table 100                 # 表 100 在
python3 /userdata/tcp_retx.py           # TCP 重传计数
/etc/init.d/S23web status               # 网页在监听
curl -s http://127.0.0.1/api/status     # 状态可读
```

**判据**：`tun_stats` 的 `fail` 应长期为 0（帧级重传会补掉孤立失败），
`retry_ok` 上升说明重传在救帧；`ip_drop` 应为 0。

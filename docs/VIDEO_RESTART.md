# 改图传 / 重启 rkipc 的正确姿势

> 这篇是**踩坑换来的操作规程**。我在手动重启 rkipc 时把图传搞挂过一次
> （网页黑屏），还顺带触发看门狗复位了两次板子。下面是原因和正确做法。

---

## 一、两条硬性约束

### 约束 1：rkipc 是**开机链的一部分**，它死了看门狗会复位板子

`S21wdt` 起的硬件看门狗会盯着系统。rkipc 由 `RkLunch.sh` 在开机时拉起，
是常驻关键服务。**把它杀掉而不立刻正确拉起** → 看门狗复位整块板子。

现象：你以为只是"重启个服务"，结果 `uptime` 归零、SSH 断开、整机重启。

### 约束 2：重启 rkipc 会**残留 554 监听 socket**

旧 rkipc 被 kill 后，它的 554 监听 socket 会残留在内核里。新进程 `bind` 失败：

```
[ERROR rtsp_demo.c:264:rtsp_new_demo] bind socket to address failed : Address already in use
[ERROR rtsp_demo.c:460:rtsp_new_session] param invalid
```

**结果是"进程活着但不出流"** —— `ps` 看 rkipc 在跑、`netstat` 看 554 在监听，
但 RTSP 连上去拿不到 SDP，mediamtx 一直重连，**网页黑屏**。

这个残留靠内核回收，时间不确定（实测几十秒到永不回收）。

---

## 二、还有一条：`LD_LIBRARY_PATH` 必须带

rkipc 依赖 `/oem/usr/lib` 下的 `librockit.so` 等库。手动启动时如果没带
`LD_LIBRARY_PATH`，它会立刻退出：

```
/oem/usr/bin/rkipc: can't load library 'librockit.so'
```

**注意 PATH 里也没有 `/oem/usr/bin`**，所以直接敲 `rkipc` 会 `not found`。

三组对照实测：

| 启动方式 | 结果 |
|---|---|
| `rkipc`（靠 PATH）| `rkipc: not found` |
| `cd /oem/usr/bin && ./rkipc` | `can't load library 'librockit.so'` |
| `LD_LIBRARY_PATH=/oem/usr/lib ./rkipc` | **正常** ✓ |

---

## 三、正确做法：用现成的工具，别手搓

仓库里 **`board/app/video_ctl.py` 的 `_restart_rkipc()`** 已经正确处理了
上面所有问题（踩过三次才写对）。它会：

1. **先停 mediamtx** —— 它是不停重连的源头，会加剧 socket 占用
2. 杀掉 rkipc
3. **轮询等 554 真的变成"不监听"**（最多 25 秒），而不是 sleep 一下就走
4. 带 `LD_LIBRARY_PATH` 启动 rkipc
5. 检查 bind 日志 + **真连一次 RTSP** 确认出流
6. 无论如何都把 mediamtx 起回来（否则图传彻底没了）

用法：

```python
import sys; sys.path.insert(0, "/userdata/car")
import video_ctl
ok, msg = video_ctl._restart_rkipc()      # ok=True 才算成功
```

网页上的「画质」档位切换走的也是这条路（`video_switch()`），所以
**正常切换画质是安全的**。

### 实测验证

```
=== 重启前 ===
  RTSP: OK
  rkipc: 424 root rkipc -a /oem/usr/share/iqfiles
  mediamtx: 514 root /root/mediamtx/mediamtx ...

=== 调用 video_ctl._restart_rkipc() ===
  返回: (True, 'ok')  耗时 43.5s

=== 重启后 ===
  RTSP: OK                                   ← 有流
  rkipc: 1652 root /oem/usr/bin/rkipc ...    ← 新进程
  mediamtx: 1724 root ./mediamtx ...         ← 也拉回来了
  554: LISTEN (队列 0/0)
  bind 错误: 无                              ← 没有残留问题
  uptime: 持续增长                            ← 没有触发看门狗
```

---

## 四、验证图传是否真的活着（别只看端口）

**只看 `netstat` 有 554、或 WHEP 返回 204 是不够的** —— 那只说明
"端口在监听 / API 在响应"，不代表有视频流。

真正判据是**拉一次 SDP**：

```sh
# 有 m=video 才算真的有流
python3 -c "
import socket
s=socket.create_connection(('127.0.0.1',554),timeout=6)
s.sendall(b'DESCRIBE rtsp://127.0.0.1:554/live/0 RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n')
d=s.recv(4096).decode(errors='replace')
print('有流' if 'm=video' in d else '无流: '+d[:200])
"
```

另外 `netstat -ltn` 里 554 的 **Recv-Q 应该是 0**；如果持续有积压，
说明有连接卡住了（我遇到过 `Recv-Q=32`，那一版就是坏的）。

---

## 五、如果已经搞挂了怎么恢复

**最快的办法是重启板子**（不是重启服务）。开机链会用完整环境重新拉起
rkipc + mediamtx，比手动抢救可靠。

```sh
reboot
# 等 30~40 秒
# 然后按 §4 的方法验证真的出流
```

> 我在排查中"手动救"了两次，两次都被看门狗复位 —— 等于绕了一圈还是重启，
> 只是多花了时间。**先 reboot 反而更快。**

---

## 六、教训

1. **动开机链里的常驻服务前，先确认它的完整启动环境**（LD_LIBRARY_PATH、
   cwd、PATH）**和恢复方案**。
2. **能复用现成工具就别手搓** —— `_restart_rkipc()` 已经把三个坑都填了，
   我绕过它自己写，结果三个坑全踩了一遍。
3. **验证要验证"结果"，不是"现象"** —— 端口在监听 ≠ 有流。

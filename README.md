# Luckfox Pico Pro Max RC Car

**English** · [中文](README.zh-CN.md)

A complete implementation on a **single-core Linux board**: camera streaming, browser-based
remote control, failsafe deadman, and the core of this project — a **kernel-mode network
tunnel that uses an ESP32-C5 as an SPI slave**.

```
        ┌──────────────┐   WiFi    ┌────────────┐   SPI 20MHz   ┌─────────────────┐
        │  Browser /   │ ────────► │  ESP32-C5  │ ◄───────────► │  Luckfox RV1106 │
        │    Phone     │           │  WiFi br.  │  4096B frames │  single A7 core │
        └──────────────┘           │  + NAPT    │               │  spitun.ko      │
                                   └────────────┘               └─────────────────┘
```

The board has **no IP address on the WiFi subnet** — the SPI tunnel is its only path
to the outside world.

---

## Layout

```
firmware/c5-tunnel/     ESP32-C5 firmware (SPI slave + WiFi + NAPT)
driver/spitun.c         Board-side kernel module: the SPI tunnel (the core)
board/
  app/                  Board apps (web server / motors / video / camera / GPS)
  init.d/               Boot chain (filenames match the device EXACTLY, see below)
  config/               Config templates (car_config / mediamtx)
  dts/                  Device-tree overlay for the SPI0 + spitun node
tools/
  build/                Cross-compilation (kernel / kernel module)
  deploy/               Deploy (full deploy / module hot-swap / boot-only flash)
  diagnose/             Measurement (control latency / failsafe / SPI loss / TCP retx)
  npu/                  NPU person+pet detection (models, patched rkipc, probes)
docs/                   Documentation and screenshots
```

### Boot chain (`board/init.d/`)

The scripts invoke **each other by name** (e.g. `S24spinet_wd` calls
`/etc/init.d/S22spinet restart`), so the filenames here match the device
**one-to-one and are deliberately not renamed** — copying them over just works,
and it avoids silent failures from "repo name ≠ device name". Purpose is documented
in a `# 用途:` (purpose) comment at the top of each file:

| File | Purpose |
|---|---|
| `S20lo` | `lo` loopback (onboard services talk to 127.0.0.1) |
| `S21wdt` | Hardware watchdog (resets the board if the kernel hangs) |
| `S22spinet` | **SPI tunnel interface**: address `spitun0` + install the policy route |
| `S23web` | Onboard web control service (`web_server.py`, listens on :80) |
| `S24spinet_wd` | **Tunnel watchdog**: restarts on hang/module loss, fixes the route |
| `S25mediamtx` | Video service (pulls rkipc's RTSP, serves WebRTC/HLS) |

> **Clock**: this board **deliberately has no timezone** and no time-sync script.
> `/etc/TZ`, `/etc/localtime`, `S99rtcinit` and `S49ntp` were all removed.
> `RTC == system clock == Beijing wall-clock reading`, zero conversion, so the
> camera's burned-in OSD timestamp is simply correct.
> Root cause and evidence: [`docs/TIME.md`](docs/TIME.md) —
> **it took five failed attempts to find the real cause; worth a read.**

> Note: "spinet" in `S22spinet` / `S24spinet_wd` is a **historical name** (the tunnel
> used to be a userspace Python process, `spinet.py`). The tunnel now lives in the
> kernel; these two scripts only configure the interface and run the watchdog.
> The names stay because that is what the device calls them.

### Where to look

| Interested in | Read |
|---|---|
| How the tunnel works and why | `driver/spitun.c` + `docs/SPI_TUNNEL_DESIGN.md` |
| Measured latency-bottleneck analysis | `docs/SPI_LATENCY_ANALYSIS.md` |
| **Why the clock has no timezone** | `docs/TIME.md` |
| **The correct way to restart rkipc** | `docs/VIDEO_RESTART.md` (a procedure born from mistakes) |
| **NPU person+pet detection (live)** | `docs/NPU_DETECTION.md` (measured: works, persistent, +3ms latency) |
| **Deploy "didn't take effect" — check first** | `docs/USERDATA_SPACE.md` (`/userdata` is only 2.2MB) |
| Full development log and pitfalls | `docs/PROGRESS.md` |
| Hardware wiring | `docs/WIRING.md` |
| Deploying to the board | `board/init.d/` + `tools/deploy/` |
| Debugging | `docs/ISSUES.md` + `tools/diagnose/` |

---

## Interface

Browser client (same page for PC and phone, responsive):

![Control UI - desktop](docs/images/ui-desktop.png)

| Mobile | Telemetry / debug panels |
|---|---|
| ![Control UI - mobile](docs/images/ui-mobile.png) | ![Debug panels](docs/images/ui-debug-panels.jpg) |

### System status

A single **compact chip strip** (measured **21px** tall — 86% smaller than the first
version's card):

```
[ CPU 43% ] [ MEM 47M ] [ NPU in use ] [ DET 15fps ]   temp 47° · load 11.8 · disk 108.7M · up 8m
```

The page reads a `sys` snapshot that the board collects inside its **existing 1Hz
telemetry loop** — the browser never triggers collection itself, so this adds
**zero** load to the board (and ~570 bytes to the response).

Design trade-offs:

* **No progress bars** — pressure is expressed by **value colour** instead. Colour is
  enough to answer "is anything wrong?", and dropping four bars saves real height.
* **No heading** — the chips carry their own labels.
* **Missing processes only appear when missing** — normally the strip stays short;
  if a service dies the secondary line appends `missing: streaming,web` and turns red.

⚠️ Two opposite meanings — the thresholds must stay separate (sharing them caused a bug):

| Kind | Meaning | Colours |
|---|---|---|
| CPU / memory | **Utilisation** | more = worse (≥70% yellow, ≥90% red) |
| NPU / detection | **On/off state** | healthy green / unconfirmed yellow / fault red |

> The first version shared the utilisation colouring across all four rows, so "NPU in
> use" was marked red — it looked like a fault. Caught by pixel-analysing a screenshot.

> Another pitfall: `web_server.py`'s `/proc/<pid>/comm` is **`python3`** (comm is the
> thread name, not the script name), so matching on comm made the "web" service look
> permanently missing and the strip warned red forever. It now re-reads `cmdline`
> for `python*` processes to get the script name.


### On-screen controls (OSD)

Controls are styled like a **video player's OSD** (they fade in when you hover or tap
the video) and are deliberately small so they don't cover the picture:

* **Top-left HUD (always visible)**: `RTT xxms · video …`, placed just below the
  camera's burned-in watermark so the two don't overlap. RTT is the true round-trip of
  a control command — the fastest way to see whether the control link is healthy.
  It **stays visible even when video is down** (`0B/s`), because that is exactly when
  you need to know whether control still works. Colour-coded: <80ms green, <200ms yellow, above red.
* **Bottom control bar**: tapping "exposure 1/1000" or "quality 720p" opens a
  **pop-up menu** (semi-transparent, the video shows through), which closes on select.
* **Failsafe warning** — the board tracks the fraction of command intervals that exceed
  the deadman timeout ("false-stop rate") and turns the page red above 5%.

### Controls: press, don't drag

The panel has exactly **one** slider (speed) and **one** big button:

* **Spin in place**: two **rounded isosceles right triangles** anchored to the
  **top-left / top-right corners of the joystick disc**. The right angle is at the
  outer top corner and the hypotenuse slopes inward-down (left `◤`, right `◥`, strictly
  mirrored), each containing 左/右 (left/right) text placed on the triangle's
  **centroid** (measured centring error: 0.0px).

  ```
  ╭──────────╮
   ╲  left   │
    ╲        │
     ╲       │
      ╲      │
  ```

  Sitting on the disc's corners and sized generously (86px on mobile) means both thumbs
  land there naturally without moving your palm. The triangles sit over the disc's
  **empty bounding-box corners** (222px from centre vs a 150px radius, measured), so
  they never block the joystick — the centre and the "forward" zone remain clickable.
  This replaced a rotation *slider*: holding a slider position one-handed is awkward,
  whereas **hold to turn, release to recentre** is both better feel and a natural
  failsafe. Turn amount ramps smoothly to ±70% over ~700ms and the triangle turns blue
  while held.

  > Implemented with **`clip-path: path()`** (SVG path) rather than `polygon()`:
  > `polygon()` has only sharp corners and **cannot round them**, while `path()`
  > supports arcs, letting each corner get its own radius (r=12 at the right angle,
  > r=4 at the two acute corners). `border`-drawn triangles were also ruled out —
  > they are pseudo-elements and **cannot contain text**.

* **"■ Stop / recentre" is one button doing both jobs.** It used to be three buttons
  (stop / recentre stick / brake), but while driving, "stop" and "recentre" are always
  the same action. Splitting them just invites a mis-tap under pressure. The single
  button zeroes the motion vector → **clears keyboard state** → visually recentres the
  knob → stops the spin and sends `stop`.

> **Three pitfalls, all reproduced on hardware**:
>
> 1. **`setPointerCapture` makes `pointerleave` never fire.** The triangle buttons
>    originally captured the pointer, which broke "hold, then drag the pointer away" —
>    the car kept spinning. That is dangerous on an RC car. Now there is no capture;
>    `pointerup/pointercancel` are bound on `window` and `pointerleave` covers the rest.
> 2. **The stop button must clear keyboard state.** Otherwise "hold `W`, then click stop"
>    makes the car **immediately drive again** (`w` is still in `keys`, and the next
>    `keyApply` pushes it back).
> 3. **`border`-drawn triangles with two transparent sides give an isosceles triangle**,
>    and pseudo-elements cannot hold text. Shape is therefore cut from the button itself
>    with `clip-path`. (There was also a detour: assembling "vertical edge against the
>    disc" arrows out of borders — correct direction, wrong shape and position.)

> **Why exposure/quality are presets, not sliders**: ISP parameters are latched when
> rkipc starts, so changing any camera parameter requires **restarting rkipc (~20s of
> video interruption)** — a hard constraint. Hence discrete presets.
> Exposure presets are measurably effective (1/1000 → ISP exposure=41, 1/25 → 1624,
> monotonic and controllable), whereas writing `/dev/v4l-subdev2` directly for a "live
> slider" is **completely ineffective** on this board — the ISP auto-exposure overwrites
> it within 800ms. That code was removed.

---

## Why it's worth a look

Most of this code was not "written" so much as **forced out by three constraints:
one CPU core, no link-layer retransmission, and no IP path**. Every non-obvious design
decision has **measured data and the mistakes that led to it** recorded in the comments.

### 1. The SPI tunnel lives in the kernel (`driver/spitun.c`)

It used to be userspace Python (`spinet.py`), which **burned 25–36% CPU busy-polling**
on the single A7 core and starved the video encoder. Moving it into the kernel dropped
CPU usage to ~0% and restored the frame rate.

- **Pack multiple IP packets per frame**: frames are a fixed 4096B and one exchange
  costs 3.1ms. Carrying a single 1350B packet wastes two thirds of every frame →
  pack 3, throughput ×2.5
- **Small-packet priority queue**: control commands/ACKs are tens of bytes and should
  not queue behind 1350B video packets
- **Frame-level retransmission**: the SPI slave's "arming window" colliding with a host
  transfer loses an entire frame (measured 0.6–4% steady state, **30% bursts** during
  boot). The tunnel has no retransmission → a lost frame = the packet is gone forever →
  you wait for TCP's **RTO ≥200ms**. Retransmitting at the **lowest layer** (~3ms cost)
  recovered **all** boot-phase failures (`retry=5, retry_ok=5, fail=0`)
- **Retry budget**: when the C5 is entirely offline every frame fails, and blind retries
  just spin the main loop (measured 6183 pointless retries) → a budget hands control
  back to the upper layer

### 2. Return routing uses **source-based policy routing**, not a hardcoded client IP

The board has two paths (eth0 cable / spitun0 tunnel) and a mis-routed reply means total
loss of contact. It used to hardcode `192.168.3.64`; when the client's DHCP lease changed
(to `.65`) control went **completely dead** while the board looked perfectly healthy
(C5 online, tunnel up, CPU idle, local API 12ms).

Now traffic is split by **source address**, so it never needs to know who the client is:

```sh
ip rule add from 10.77.0.2 lookup 100 pref 100
ip route replace 192.168.3.0/24 dev spitun0 src 10.77.0.2 table 100
```

> A dead end worth recording: "self-healing the route per request" inside web_server —
> **does not work**. The SYN-ACK is emitted by the kernel; without a route the handshake
> never completes and the handler is never invoked.

### 3. Failsafe deadman + leading-edge heartbeat

The page used to send commands **only when a value changed**, so holding the stick still
meant no heartbeat, the board stopped the car, and the symptom was "the car stutters".
Now: **send immediately on any input change + a 100ms heartbeat + request timeout**,
with a configurable timeout (`failsafe_s` in `board/config/car_config.example.json`) and
page version self-check (a stale page auto-reloads).

### 4. On one core, every millisecond counts

The comments contain many "this used to cost X% CPU" records: software PWM dropped from
1kHz to 250Hz; telemetry collection dropped from 5Hz back to 1Hz (`read_adc_mv` costs
264ms per call — at 5Hz that is 132% of one core, physically impossible); disabling
Nagle saved 36ms per command.

---

## Key measured numbers

| Metric | Value |
|---|---|
| SPI single frame | 3.10 ms (1.64ms on the wire @20MHz, **1.46ms fixed overhead**) |
| Tunnel throughput | 3.42 Mbps before → **9.21 Mbps** after |
| Control latency (loaded) | p50 95ms → **p50 36ms / p90 61ms / max 78ms** |
| Control packet loss (loaded) | long tail → **200/200 delivered, 0 loss** |
| Frame failures (boot phase) | tens of thousands → **fail=0** (all recovered by retransmit) |
| Control latency (idle) | p50 **24ms**, local loopback 12ms |
| NPU detection (person+pet) | p50 29ms at `npu_fps=15`; **+3ms** vs no detection |

---

## Hardware

| Part | Model |
|---|---|
| Main SoC | Luckfox Pico Pro Max (RV1106, single Cortex-A7, 128MB) |
| WiFi bridge | ESP32-C5 (SPI slave + NAPT) |
| Camera | SC3336 3MP (CSI; H.264 encoding done by rkipc) |
| Chassis | 4WD mecanum wheels + TB6612 ×2 (MD240A) |
| Video | rkipc (RTSP) → mediamtx (WebRTC / HLS) |
| NPU | RV1106 NPU (0.5 TOPS) via rockiva, PFP model (Person/Face/Pet) |

> ⚠️ **Motors must be powered independently.** Running motors off USB/debug power makes
> inrush current collapse the rail → board/C5 brown out → the tunnel drops for 2 seconds.
> The C5's serial log has direct evidence: `E BOD: Brownout detector was triggered`.

---

## Deploy

**1. Configuration** (real credentials are not in this repo; fill in the templates):

```sh
cp board/config/car_config.example.json /userdata/car/car_config.json
# fill in MQTT broker / password / pins
cp firmware/c5-tunnel/main.c.example firmware/c5-tunnel/main.c
# fill in WiFi SSID / password, then idf.py build flash
```

**2. Board side**:

```sh
# apps
scp board/app/* root@<board-ip>:/userdata/car/
# boot chain (init.d filenames must match the device exactly)
scp board/init.d/S* root@<board-ip>:/etc/init.d/
ssh root@<board-ip> "chmod 755 /etc/init.d/S*; reboot"
# device-tree overlay
scp board/dts/spi0-tunnel.dts ...     # compile to .dtbo, loaded by hwcfg
```

**3. Kernel module** (must be built against a kernel matching the running one):

```sh
tools/build/build-spitun.sh        # run in WSL; produces spitun.ko
tools/deploy/reload-tunnel.sh      # hot-swap (drops the link for seconds, auto-rollback)
```

**4. NPU detection** (optional): see [`docs/NPU_DETECTION.md`](docs/NPU_DETECTION.md).
Summary: copy `object_detection_pfp.data` to `/usr/lib/`, set `enable_npu = 1` and
`npu_fps = 15`, and (to also draw pet boxes) install the patched rkipc from
`tools/npu/rkipc-pet-6/`.

---

## Diagnostic tools

| Tool | Purpose |
|---|---|
| `tools/diagnose/measure-control-latency.sh` | Control round-trip latency breakdown |
| `tools/diagnose/check-video-stream.sh` | Verify RTSP really produces a stream (not just a listening port) |
| `tools/diagnose/test-failsafe.sh` | Failsafe verification (covers both false and missed stops) |
| `tools/diagnose/watch-failsafe.py` | Board-side observation of failsafe trips and C5 resets |
| `tools/diagnose/watch-spi-loss.py` | SPI frame failure rate / retransmissions |
| `tools/diagnose/tcp-retransmits.py` | TCP retransmit counters (indirect evidence of loss) |
| `tools/diagnose/check-boot-time.py` | Verify the clock is right **at boot** (not just after) |
| `tools/diagnose/measure-frame-exposure.py` | Objective over/under-exposure measurement |
| `tools/npu/measure-control-latency.py` | Latency measurement with baseline comparison |
| `tools/npu/probe-npu-stack.sh` | Check the NPU stack is complete |
| `tools/npu/watch-crash-log.sh` | Persist dmesg to SD so a crash survives a reboot |
| `tools/npu/rknn_probe.c` | Cross-compiled RKNN probe (validates the inference path) |
| `tools/npu/verify-npu-detection.sh` | NPU detection acceptance test |

---

## Licence

For study and reference only. This involves real hardware — assess safety yourself,
and **always get the failsafe working before running the car**.

# Luckfox Pico Pro Max RC Car

**English** · [中文](README.zh-CN.md)

A complete implementation on a **single-core Linux board**: low-latency video
streaming, browser remote control, failsafe deadman, NPU person/pet detection,
GPS — and the core of the project: a **network tunnel that uses an ESP32-C5 as
an SPI slave to put a board with no wireless interface onto WiFi**.

```
      ┌──────────────┐   WiFi    ┌────────────┐  SPI 20MHz    ┌─────────────────┐
      │  Browser /   │ ────────► │  ESP32-C5  │ ◄───────────► │  Luckfox RV1106 │
      │    Phone     │  WebRTC   │  WiFi br.  │  4096B frames │  single A7 core │
      └──────────────┘  ICE-TCP  │  + NAPT    │  spinet_c     │  tun0 = 10.77.0.2
                                 └────────────┘               └─────────────────┘
```

The board has **no IP address on the WiFi subnet** — the SPI tunnel is its only
path to the outside world. Video (WebRTC) and control commands all cross that
one SPI wire.

---

## Current state (2026-10-08)

| Item | Status |
|---|---|
| Video | H.264 720p 25fps, WebRTC (ICE-TCP) through the tunnel |
| Tunnel | **C pump** (user space), 6-8% CPU under streaming, 340 frames/s, frame-level retransmit, zero loss |
| Control | p50 ~28ms, deadman failsafe (auto-stop after 0.5s without heartbeat) |
| NPU | person/face/pet detection (in-rkipc inference), pet boxes drawn, +3ms latency |
| GPS | UART1 (GPIO 68/69), NMEA parsing, position/satellites on the web page |
| Web UI | responsive (PC/phone), joystick + spin buttons + speed slider, status panels |

## Layout

```
firmware/c5-tunnel/     ESP32-C5 firmware (SPI slave + WiFi + NAPT + port maps)
board/
  app/                  Board-side applications
    spinet.py           Tunnel pump (Python version, kept as C fallback)
    web_server.py       Web server :80 (API / failsafe / telemetry cache)
    car_motor.py        4x TB6612 motors (hardware PWM + failsafe)
    sys_stats.py        System stats collection (light/heavy field split)
    index.html          Control page (single file)
    video_ctl.py        Video profile/exposure (rkipc config management)
    cam_ctl.py          Camera parameters
  spinet_c/spinet.c     Tunnel pump **in C** (current workhorse, see below)
  init.d/               Boot chain (filenames match the device EXACTLY)
  config/               Config templates (car_config / mediamtx)
  dts/                  Device-tree overlays (SPI / PWM / UART1)
driver/spitun.c         Kernel-mode tunnel (historic, replaced by spinet_c)
tools/
  build/                Cross-compilation
  deploy/               Deployment (post-flash restore / hot swap)
  diagnose/             Measurement & observation
  npu/                  NPU detection (models / patched rkipc / probes)
docs/                   Documentation (design / lessons / ops)
```

### Boot chain (`board/init.d/`, filenames match the device, drop-in usable)

| Script | Purpose |
|---|---|
| `S21spitun` | Mount configfs + apply SPI0 overlay (spidev) + load tun.ko |
| `S21uart1` | Apply UART1 overlay (GPS on GPIO 68/69) |
| `S22pwm` | Apply 4-channel hardware PWM overlay (motor speed) |
| `S22spinet` | Start **spinet_c** (falls back to spinet.py) + tun0 address + return policy route |
| `S23web` | web_server.py (:80, control + video page) |
| `S24spinet_wd` | Tunnel watchdog (relaunches the pump if it dies) |
| `S25rkipc` | Camera (starts rkipc only after the SC3336 answers on I2C) |
| `S26mediamtx` | Video service (pulls rkipc RTSP → WebRTC; HLS on demand only) |

> Naming: "spinet" in `S22spinet`/`S24spinet_wd` is a historic name (the tunnel
> used to be a Python process). The current pump is the C program built from
> `board/spinet_c/spinet.c`.

### Why the tunnel pump is C (spinet_c)

The tunnel is a **340 exchanges/s full-duplex SPI loop** — per-frame overhead
is everything:

| Implementation | CPU under streaming | Notes |
|---|---|---|
| Python (spinet.py) | 17~25% | ~0.5ms/frame interpreter tax |
| Kernel module (spitun.ko) | ~1-3% | Was deployed; not fully validated against the C5 firmware; **shelved** |
| **C user space (spinet_c)** | **6~8%** | Current: nearly all of the kernel version's benefit; a bug just kills a process that the watchdog relaunches |

spinet.py → spinet.c is a line-faithful port. All protocol details preserved:
sliding window + frame-level retransmit, 3 IP packets per frame, idle probes
that never consume a sequence number, C5-reboot resync, idle backoff
(1→4→8ms). Rollback: delete the spinet_c binary and reboot.

> ⚠️ Lesson (two incidents): **device-tree overlays and kernel modules must
> only be switched at a reboot boundary** — swapping them on a live system
> leaks IOMUX/properties and causes phantom pin failures or kernel crashes.
> See `docs/RECOVERY_REPORT.md`.

---

## Measured numbers

| Metric | Value |
|---|---|
| SPI frame | 1.64ms on the wire @20MHz, ~3ms round trip |
| Tunnel throughput | up to 9.2 Mbps (packing + sliding window) |
| Control latency (loaded) | p50 36ms / p90 61ms, 0 loss (frame retransmit) |
| Control latency (idle) | p50 ~25ms |
| Tunnel pump CPU | 6-8% streaming (Python: 17-25%) |
| NPU detection | +3ms control latency (npu_fps=15), person/face/pet |
| web_server idle | 6% (after light/heavy stats split; was 15%) |

---

## Hardware

| Part | Model / notes |
|---|---|
| SoC | Luckfox Pico Pro Max (RV1106, single Cortex-A7, 128MB) |
| WiFi bridge | ESP32-C5 (SPI slave + NAPT + port maps 80/22/554/8889/8189) |
| Camera | SC3336 3MP (CSI, H.264 encoded by rkipc) |
| Chassis | 4WD mecanum + 2× TB6612 |
| GPS | NMEA serial module (UART1, GPIO 68/69) |
| NPU | RV1106 NPU, rockiva PFP model (person/face/pet) |

### Motor wiring (final, 2026-10-08; per motor: PWM / AIN1 / AIN2)

| Wheel | PWM | AIN1 | AIN2 | pwmchip |
|---|---|---|---|---|
| FL | 57 | 56 | 72 | 10 (pwm10m2) |
| FR | 52 | 53 | 54 | 8 (pwm8m1) |
| BL | 73 | 59 | 58 | 6 (pwm6m1) |
| BR | 55 | 65 | 64 | 11 (pwm11m1) |

> ⚠️ **Unusable-pin blacklist** (measured the hard way):
> GPIO 42/43 — debug-UART pins, output driver dead (write 0 reads 1);
> GPIO 71 — no PWM function in the 250607 firmware.
> Battery ADC = SARADC_IN1 (GPIO 145 / header pin 32). Full pin table and
> change history: [`docs/WIRING.md`](docs/WIRING.md).

> ⚠️ **Motors need their own power supply.** Powering them from USB sags the
> rail → brownout → tunnel drops (C5 log: `E BOD: Brownout detector was
> triggered`).

---

## Deployment

**Full rebuild (after a re-flash)**:

```bash
# Flash: board USB into MaskRom, the SocToolKit upgrade_tool works from CLI:
#   upgrade_tool uf Luckfox_Pico_Pro_Max_Flash_250607/update.img
# Then one-shot rebuild (over adb or ssh; reinstalls every service):
tools/deploy/_postflash_restore.sh <board-ip>
# Two /userdata files still needed by hand: hwcfg/spi0_spidev.dts + hwcfg/tun.ko
```

**Day-to-day app updates**:

```bash
scp board/app/* root@<board-ip>:/userdata/car/
scp board/init.d/S* root@<board-ip>:/etc/init.d/   # filenames must match
scp board/spinet_c/spinet_c root@<board-ip>:/userdata/car/   # optional
```

**Config**: `board/config/car_config.example.json` → motor pins / MQTT / GPS
port. Real secrets never enter the repo (template says CHANGE_ME).

**NPU detection** (optional): models into `/usr/lib/`, `enable_npu=1`,
`npu_fps=15` (both the ini and the template); for pet boxes also install the
patched rkipc from `tools/npu/rkipc-pet-6/`.
See [`docs/NPU_DETECTION.md`](docs/NPU_DETECTION.md).

---

## UI

One responsive page for PC and phone:

![Control UI - desktop](docs/images/ui-desktop.png)

| Mobile | Telemetry / debug panels |
|---|---|
| ![mobile](docs/images/ui-mobile.png) | ![debug panels](docs/images/ui-debug-panels.jpg) |

The video area carries a player-style OSD: a persistent `RTT · bitrate` HUD
(color-coded), exposure/quality as pop-up gear menus (ISP parameters require an
rkipc restart — sliders are pointless), and a red warning when the failsafe
mis-stop rate exceeds 5%. Spin is two rounded-triangle buttons (hold to spin,
release to stop). Design notes and pitfalls: [`docs/PROGRESS.md`](docs/PROGRESS.md).

---

## Docs

| Doc | Content |
|---|---|
| [`docs/SPI_TUNNEL_DESIGN.md`](docs/SPI_TUNNEL_DESIGN.md) | Tunnel protocol & design |
| [`docs/SPI_LATENCY_ANALYSIS.md`](docs/SPI_LATENCY_ANALYSIS.md) | Latency breakdown, measured |
| [`docs/WIRING.md`](docs/WIRING.md) | Final wiring + unusable-pin blacklist |
| [`docs/RECOVERY_REPORT.md`](docs/RECOVERY_REPORT.md) | Re-flash rebuild / incident post-mortems / ops manual |
| [`docs/NPU_DETECTION.md`](docs/NPU_DETECTION.md) | NPU detection deployment & tuning |
| [`docs/VIDEO_RESTART.md`](docs/VIDEO_RESTART.md) | How to restart rkipc safely |
| [`docs/USERDATA_SPACE.md`](docs/USERDATA_SPACE.md) | /userdata is 2.2MB — deployment discipline |
| [`docs/TIME.md`](docs/TIME.md) | Why the clock deliberately has no timezone |
| [`docs/ISSUES.md`](docs/ISSUES.md) | Known issues |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | Full development log |

---

## Licence

For study and reference only. This involves real hardware — assess safety yourself,
and **always build the deadman failsafe first**.

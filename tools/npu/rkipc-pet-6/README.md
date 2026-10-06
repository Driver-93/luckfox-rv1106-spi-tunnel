# rkipc PET 画框补丁

原厂 rkipc 的 OSD 画框代码只处理 `PERSON / FACE / VEHICLE / NON_VEHICLE`，
**`ROCKIVA_OBJECT_TYPE_PET`（=6，猫/狗）没有分支** —— 狗会被检测到，但不画框。

本目录提供：

| 文件 | 说明 |
|---|---|
| `video.c.pet.patch` | 加 PET 分支的补丁（对着 `rkipc/src/rv1106_ipc/video/video.c`）|
| `rkipc` | 编译并 strip 好的二进制（461648 字节，与原厂同尺寸）|

补丁内容（只加了一个 else-if）：

```c
} else if (object->objInfo.type == ROCKIVA_OBJECT_TYPE_PET) {
    draw_rect_2bpp((RK_U8 *)stCanvasInfo.u64VirAddr, stCanvasInfo.u32VirWidth,
                   stCanvasInfo.u32VirHeight, x, y, w, h, line_pixel,
                   RGN_COLOR_LUT_INDEX_1);   // 红框
}
```

## 注意

* **只有 `RGN_COLOR_LUT_INDEX_0`(蓝) 和 `_1`(红) 两个颜色**，没有 `_2`。
  用 `_1` 和 VEHICLE 同色（原厂既有做法）。
* 颜色对应：人/脸 = 蓝，车/非机动车/宠物 = 红。

## 应用补丁

```bash
cd <sdk>/project/app/rkipc/rkipc/src/rv1106_ipc/video
patch -p0 < video.c.pet.patch

cd <sdk>/project/app/rkipc/build
export PATH=<sdk>/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH
make rkipc -j4

# 必须 strip, 否则体积对不上 (644KB vs 板子 461KB)
arm-rockchip830-linux-uclibcgnueabihf-strip src/rv1106_ipc/rkipc
```

`build/` 目录在 SDK 里是现成的（`CMakeCache.txt` 已指向 uclibc 工具链），
所以是增量编译，很快。

## 直接部署现成二进制

```sh
# 先备份 (回滚点)
cp /oem/usr/bin/rkipc /mnt/sdcard/npu/rkipc_stock_backup

# 装新的
cp rkipc /oem/usr/bin/rkipc && chmod +x /oem/usr/bin/rkipc

# 用正确方式重启 (不要手搓 kill! 见 docs/VIDEO_RESTART.md)
python3 -c "
import sys; sys.path.insert(0,'/userdata/car'); import video_ctl
print(video_ctl._restart_rkipc())"
```

## 校验

```sh
md5sum /oem/usr/bin/rkipc
# 带 PET 分支: c13eff703c8b6cc52726c4118db3c719
# 原厂版:      ecd4991ee05a6236a29b9ba4c7218fd4
```

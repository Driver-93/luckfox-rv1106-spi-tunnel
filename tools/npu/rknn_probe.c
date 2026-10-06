/*
 * rknn_probe.c —— 用 SDK 的真实 rknn_api.h 调 RKNN, 验证 NPU 推理链路。
 *
 * 为什么用 C 而不是 ctypes:
 *   ctypes 调 rknn_query 会段错误 (即使按头文件修正了 cmd 枚举和结构体大小)。
 *   用 C 编译时由编译器按头文件生成正确的调用约定/结构体布局, 排除 ABI 猜测。
 *
 * 编译 (WSL, SDK 工具链):
 *   arm-rockchip830-linux-uclibcgnueabihf-gcc rknn_probe.c -o rknn_probe \
 *       -I<sdk>/project/app/rk_smart_door/smart_door/common/face/algo \
 *       -L<path> -lrknnmrt
 *
 * 用途: 板上运行, 报告 SDK 版本 / 输入输出张量属性 / 一次推理耗时。
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "rknn_api.h"

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char **argv)
{
    const char *model_path = (argc > 1) ? argv[1]
                                        : "/mnt/sdcard/npu/model/yolov5.rknn";
    printf("=== rknn_probe ===\n");
    printf("model: %s\n", model_path);

    FILE *fp = fopen(model_path, "rb");
    if (!fp) {
        printf("ERROR: cannot open model: %s\n", strerror(errno));
        return 1;
    }
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    printf("model size: %ld bytes\n", sz);

    unsigned char *data = malloc(sz);
    if (!data) { printf("ERROR: malloc failed\n"); fclose(fp); return 1; }
    if (fread(data, 1, sz, fp) != (size_t)sz) {
        printf("ERROR: read failed\n"); fclose(fp); free(data); return 1;
    }
    fclose(fp);

    /* ---------- 1) 初始化 ---------- */
    rknn_context ctx = 0;
    int ret = rknn_init(&ctx, data, (uint32_t)sz, 0, NULL);
    printf("rknn_init ret=%d ctx=%u\n", ret, (unsigned)ctx);
    if (ret < 0) { printf("ERROR: rknn_init failed\n"); return 1; }

    /* ---------- 2) SDK 版本 (cmd=5) ---------- */
    rknn_sdk_version ver;
    memset(&ver, 0, sizeof(ver));
    ret = rknn_query(ctx, RKNN_QUERY_SDK_VERSION, &ver, sizeof(ver));
    printf("query SDK_VERSION ret=%d\n", ret);
    if (ret == 0) {
        printf("  api_version = %s\n", ver.api_version);
        printf("  drv_version = %s\n", ver.drv_version);
    }

    /* ---------- 3) 输入输出数量 ---------- */
    rknn_input_output_num io_num;
    memset(&io_num, 0, sizeof(io_num));
    ret = rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
    printf("query IN_OUT_NUM ret=%d  n_input=%u n_output=%u\n",
           ret, io_num.n_input, io_num.n_output);
    if (ret < 0) { rknn_destroy(ctx); return 1; }

    /* ---------- 4) 输入张量属性 ---------- */
    for (uint32_t i = 0; i < io_num.n_input; i++) {
        rknn_tensor_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.index = i;
        ret = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &attr, sizeof(attr));
        printf("input[%u] ret=%d n_dims=%u dims=[", i, ret, attr.n_dims);
        for (uint32_t d = 0; d < attr.n_dims; d++)
            printf("%u%s", attr.dims[d], d + 1 < attr.n_dims ? "," : "");
        printf("] fmt=%u type=%u n_elems=%u size=%u\n",
               attr.fmt, attr.type, attr.n_elems, attr.size);
    }

    /* ---------- 5) 输出张量属性 ---------- */
    for (uint32_t i = 0; i < io_num.n_output; i++) {
        rknn_tensor_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.index = i;
        ret = rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &attr, sizeof(attr));
        printf("output[%u] ret=%d n_dims=%u dims=[", i, ret, attr.n_dims);
        for (uint32_t d = 0; d < attr.n_dims; d++)
            printf("%u%s", attr.dims[d], d + 1 < attr.n_dims ? "," : "");
        printf("] size=%u\n", attr.size);
    }

    /* ---------- 6) 跑一次推理 (用中性灰输入, 只测耗时/链路) ---------- */
    rknn_tensor_attr in_attr;
    memset(&in_attr, 0, sizeof(in_attr));
    in_attr.index = 0;
    rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr));

    /*
     * 输入缓冲大小 = w*h*c (uint8, NHWC)。
     * 注意: attr.size 是**模型期望**的字节数 (量化后), 而我们要喂的是
     * uint8 NHWC 原始图像, 所以用 dims 自己算, 并且要传 type=RKNN_TENSOR_UINT8
     * + fmt=RKNN_TENSOR_NHWC, 让驱动去做量化转换 (pass_through = 0)。
     */
    uint32_t W = 640, H = 640, C = 3;
    if (in_attr.n_dims == 4) {
        /* dims 布局按模型而定; 我们的模型查到是 [1,640,640,3] (NHWC) */
        if (in_attr.dims[3] == 3 || in_attr.dims[3] == 1) {
            H = in_attr.dims[1]; W = in_attr.dims[2]; C = in_attr.dims[3];
        } else {
            C = in_attr.dims[1]; H = in_attr.dims[2]; W = in_attr.dims[3];
        }
    }
    uint32_t in_size = W * H * C;
    printf("input buf: %ux%ux%u = %u bytes (attr.size=%u)\n",
           W, H, C, in_size, in_attr.size);

    unsigned char *inbuf = malloc(in_size);
    if (!inbuf) { printf("ERROR: inbuf malloc\n"); rknn_destroy(ctx); return 1; }
    memset(inbuf, 114, in_size);   /* 中性灰 */

    rknn_input inputs[1];
    memset(inputs, 0, sizeof(inputs));
    inputs[0].index = 0;
    inputs[0].type = RKNN_TENSOR_UINT8;
    inputs[0].size = in_size;
    inputs[0].fmt = RKNN_TENSOR_NHWC;
    inputs[0].buf = inbuf;
    inputs[0].pass_through = 0;

    ret = rknn_inputs_set(ctx, 1, inputs);
    printf("rknn_inputs_set ret=%d\n", ret);
    if (ret < 0) {
        printf("  -> 错误码 %d = %s\n", ret,
               ret == -5 ? "RKNN_ERR_PARAM_INVALID" :
               ret == -8 ? "RKNN_ERR_INPUT_INVALID" :
               ret == -7 ? "RKNN_ERR_CTX_INVALID" : "见 rknn_api.h");
        free(inbuf); rknn_destroy(ctx); return 1;
    }

    double t0 = now_ms();
    ret = rknn_run(ctx, NULL);
    double t_run = now_ms() - t0;
    printf("rknn_run ret=%d  ** 推理耗时 %.1f ms **\n", ret, t_run);
    if (ret < 0) { rknn_destroy(ctx); return 1; }

    rknn_output outputs[8];
    memset(outputs, 0, sizeof(outputs));
    uint32_t nout = io_num.n_output > 8 ? 8 : io_num.n_output;
    for (uint32_t i = 0; i < nout; i++) outputs[i].want_float = 1;

    t0 = now_ms();
    ret = rknn_outputs_get(ctx, nout, outputs, NULL);
    double t_out = now_ms() - t0;
    printf("rknn_outputs_get ret=%d  (%.1f ms)\n", ret, t_out);
    if (ret == 0) {
        for (uint32_t i = 0; i < nout; i++)
            printf("  out[%u] size=%u buf=%p\n", i, outputs[i].size,
                   outputs[i].buf);
        rknn_outputs_release(ctx, nout, outputs);
    }

    printf("=== 推理链路 OK (run %.1f ms, 取输出 %.1f ms) ===\n",
           t_run, t_out);

    free(inbuf);
    rknn_destroy(ctx);
    free(data);
    printf("=== done ===\n");
    return 0;
}

/*
 * netlog.h — 把 C3 的日志与状态经 WiFi 发出去, 便于不插线调试。
 *
 * 目的: 烧在车上的 C3 平时够不着串口, 出问题时只能拆下来接 Type-C。
 * 这里做一个极轻量的 UDP 输出:
 *
 *   1. 日志转发 —— 通过 esp_log_set_vprintf() 挂上钩子, 把 ESP_LOGx 的
 *      输出同时发到 UDP 广播端口。不改任何现有 ESP_LOG 调用。
 *   2. 状态查询 —— 收到 "stat" 请求就回一行 JSON, 方便脚本拉取。
 *
 * 设计取舍:
 *   - 用 UDP 而非 TCP: 日志是"尽力而为"的, 丢几条无所谓; TCP 还要维护
 *     连接状态, 在只有 ~150KB 堆的 C3 上不值得。
 *   - 用广播而不是单播: C3 不知道谁在看日志, 广播让任意电脑都能收。
 *   - 发送放在独立的低优先级任务 + 队列里, 绝不阻塞 tunnel/revproxy 这类
 *     实时路径。队列满了就丢, 宁可少日志也不能拖慢隧道。
 */
#ifndef NETLOG_H
#define NETLOG_H

#include "esp_err.h"

/* 启动 UDP 日志转发。port=0 用默认值 NETLOG_DEFAULT_PORT。
 * 失败只影响调试便利性, 绝不能让主功能起不来, 所以调用方不必 ERROOR_CHECK。 */
esp_err_t netlog_start(uint16_t port);

/* 手动发一条 (状态上报用, 复用同一队列) */
void netlog_send(const char *s, int len);

/* 记录当前 IP, 供状态应答使用 */
void netlog_set_ip(const char *ip);

/* 生成一行状态 JSON 到 out (供 UDP "stat" 查询应答) */
void netlog_stat_line(char *out, int n);

#define NETLOG_DEFAULT_PORT 9999

#endif /* NETLOG_H */

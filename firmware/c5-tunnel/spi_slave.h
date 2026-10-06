#pragma once
#include "esp_err.h"
#include "tunnel.h"

esp_err_t spi_slave_init(void);

/* 预武装槽位数 (见 spi_slave.c 文件头) */
int spi_slave_queued_count(void);

/* 兼容旧接口: 武装"当前槽"。新代码请用 spi_slave_arm_slot()。 */
esp_err_t spi_slave_arm(void);

/* 把指定槽位入队。上层复用完 wait_rx 交回的槽, 用这个重新武装。 */
esp_err_t spi_slave_arm_slot(int slot);

/* 设置下一帧传输的字节数。变长方案已废弃, 现在是空实现。 */
void spi_slave_set_txlen(uint32_t n);

/* 等 master 发起一次传输。
 *   *rxbuf    -> 收到的数据 (指向该槽的 rx 缓冲)
 *   *slot_out -> 完成的槽号, 用完 spi_slave_txbuf() 后
 *                spi_slave_arm_slot(slot) 重新入队 */
esp_err_t spi_slave_wait_rx(uint8_t **rxbuf, int *slot_out, int timeout_ms);

/* 旧接口, 槽位池实现下是空操作 */
void spi_slave_next(void);

bool spi_slave_is_armed(void);

uint8_t *spi_slave_txbuf(void);
uint8_t *spi_slave_rxbuf(void);

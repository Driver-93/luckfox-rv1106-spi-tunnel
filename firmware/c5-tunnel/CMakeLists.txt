idf_component_register(SRCS "main.c"
                            "spi_slave.c"
                            "tunnel.c"
                            "spinet.c"
                            "netlog.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi nvs_flash esp_netif lwip driver esp_event)

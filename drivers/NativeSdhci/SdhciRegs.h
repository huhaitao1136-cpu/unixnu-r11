/*
 * SdhciRegs.h — SDHCI 3.0 标准寄存器 + Qualcomm msm-v5 私有部分的偏移定义
 *
 * 标准部分的数值来自 SD Host Controller Simplified Spec 3.00，可以放心用。
 * msm-v5 的 vendor 寄存器（PHY 时序 / DLL / vendor caps）在 0xC0C5000 那个
 * 独立 4 KiB 窗口里，偏移必须对着 msm-4.4/drivers/mmc/host/sdhci-msm.c 抄，
 * 这里只列出会用到的少量项，标 TODO 的没有经过实机核对。
 */
#ifndef NATIVE_SDHCI_REGS_H
#define NATIVE_SDHCI_REGS_H

/* ---- 板级常量（OPPO R11 实测，见 docs/hw-r11.md §10） ---- */
#define R11_SDHCI_CORE_PHYS    0x0C0C4000ULL   /* eMMC core 寄存器 */
#define R11_SDHCI_VENDOR_PHYS  0x0C0C5000ULL   /* msm v5 私有寄存器 */
#define R11_SDHCI_REG_SIZE     0x1000ULL
#define R11_SDHCI_IRQ_CORE     110             /* 0x6E */
#define R11_SDHCI_IRQ_VENDOR   112             /* 0x70 */

/* ---- 标准寄存器（偏移） ---- */
#define SDHCI_DMA_ADDRESS          0x00
#define SDHCI_BLOCK_SIZE           0x04
#define SDHCI_BLOCK_COUNT          0x06
#define SDHCI_ARGUMENT             0x08
#define SDHCI_TRANSFER_MODE        0x0C
#define SDHCI_COMMAND              0x0E
#define SDHCI_RESPONSE             0x10    /* 16 字节：RESP0..RESP3 */
#define SDHCI_BUFFER               0x20
#define SDHCI_PRESENT_STATE        0x24
#define SDHCI_HOST_CONTROL         0x28
#define SDHCI_POWER_CONTROL        0x29
#define SDHCI_BLOCK_GAP_CONTROL    0x2A
#define SDHCI_WAKE_UP_CONTROL      0x2B
#define SDHCI_CLOCK_CONTROL        0x2C
#define SDHCI_TIMEOUT_CONTROL      0x2E
#define SDHCI_SOFTWARE_RESET       0x2F
#define SDHCI_NORMAL_INT_STATUS    0x30
#define SDHCI_ERROR_INT_STATUS     0x32
#define SDHCI_NORMAL_INT_ENABLE    0x34
#define SDHCI_ERROR_INT_ENABLE     0x36
#define SDHCI_NORMAL_INT_SIGNAL    0x38
#define SDHCI_ERROR_INT_SIGNAL     0x3A
#define SDHCI_AUTO_CMD_STATUS      0x3C
#define SDHCI_HOST_CONTROL2        0x3E
#define SDHCI_CAPABILITIES         0x40
#define SDHCI_CAPABILITIES_1       0x44
#define SDHCI_MAX_CURRENT          0x48
#define SDHCI_ADMA_ERROR           0x54
#define SDHCI_ADMA_ADDRESS         0x58
#define SDHCI_PRESET_FOR_INIT      0x60
#define SDHCI_PRESET_FOR_DS        0x64
#define SDHCI_PRESET_FOR_HS        0x68
#define SDHCI_PRESET_FOR_SDR12     0x60
#define SDHCI_PRESET_FOR_SDR25     0x64
#define SDHCI_PRESET_FOR_SDR50     0x68
#define SDHCI_PRESET_FOR_SDR104    0x6C
#define SDHCI_PRESET_FOR_DDR50     0x70
#define SDHCI_PRESET_FOR_HS200     0x74
#define SDHCI_PRESET_FOR_HS400     0x78
#define SDHCI_SLOT_INT_STATUS      0xFC
#define SDHCI_HOST_VERSION         0xFE

/* PRESENT_STATE */
#define SDHCI_CMD_INHIBIT          (1U << 0)
#define SDHCI_DATA_INHIBIT         (1U << 1)
#define SDHCI_DOING_WRITE          (1U << 8)
#define SDHCI_DOING_READ           (1U << 9)
#define SDHCI_SPACE_AVAILABLE      (1U << 10)
#define SDHCI_DATA_AVAILABLE       (1U << 11)
#define SDHCI_CARD_INSERTED        (1U << 16)
#define SDHCI_CARD_STABLE          (1U << 17)

/* SOFTWATE_RESET */
#define SDHCI_RESET_ALL            0x01
#define SDHCI_RESET_CMD            0x02
#define SDHCI_RESET_DATA           0x04

/* CLOCK_CONTROL */
#define SDHCI_CLOCK_INT_EN         0x0001
#define SDHCI_CLOCK_INT_STABLE     0x0002
#define SDHCI_CLOCK_CARD_EN        0x0004
#define SDHCI_PROG_CLOCK_MODE      0x0020
#define SDHCI_CLOCK_SDCLK_DIV(_v)  ((_v) << 8)

/* POWER_CONTROL (0x29)：bit0 BUS_POWER_ON，bits[3:1] 电压选择（3.3V = 0b111） */
#define SDHCI_POWER_ON             0x01
#define SDHCI_POWER_330            0x0E
#define SDHCI_POWER_300            0x0C
#define SDHCI_POWER_180            0x0A

/* HOST_CONTROL */
#define SDHCI_CTRL_LED             0x01
#define SDHCI_CTRL_4BITBUS         0x02
#define SDHCI_CTRL_HISPD           0x04
#define SDHCI_CTRL_DMA_MASK        0x18
#define SDHCI_CTRL_8BITBUS         0x20

/* HOST_CONTROL2 */
#define SDHCI_CTRL_UHS_MASK        0x0007
#define SDHCI_CTRL_UHS_SDR12       0x0000
#define SDHCI_CTRL_UHS_SDR25       0x0001
#define SDHCI_CTRL_UHS_SDR50       0x0002
#define SDHCI_CTRL_UHS_SDR104      0x0003
#define SDHCI_CTRL_UHS_DDR50       0x0004
#define SDHCI_CTRL_HS400           0x0005
#define SDHCI_CTRL_VDD_180         0x0008
#define SDHCI_CTRL_EXEC_TUNING     0x0040

/* NORMAL_INT_STATUS */
#define SDHCI_INT_RESPONSE         0x00000001
#define SDHCI_INT_DATA_END         0x00000002
#define SDHCI_INT_DMA_END          0x00000008
#define SDHCI_INT_SPACE_AVAIL      0x00000010
#define SDHCI_INT_DATA_AVAIL       0x00000020
#define SDHCI_INT_CARD_INSERT      0x00000040
#define SDHCI_INT_CARD_REMOVE      0x00000080
#define SDHCI_INT_CARD_INT         0x00000100
#define SDHCI_INT_ERROR            0x00008000
#define SDHCI_INT_TIMEOUT          0x00010000
#define SDHCI_INT_CRC              0x00020000
#define SDHCI_INT_END_BIT          0x00040000
#define SDHCI_INT_INDEX            0x00080000
#define SDHCI_INT_DATA_TIMEOUT     0x00100000
#define SDHCI_INT_DATA_CRC         0x00200000
#define SDHCI_INT_DATA_END_BIT     0x00400000
#define SDHCI_INT_BUS_POWER        0x00800000
#define SDHCI_INT_ALL_ERRORS       0x00FF0000

/* COMMAND */
#define SDHCI_CMD_RESP_NONE        0x00
#define SDHCI_CMD_RESP_LONG        0x01
#define SDHCI_CMD_RESP_SHORT       0x02
#define SDHCI_CMD_RESP_SHORT_BUSY  0x03
#define SDHCI_CMD_RESP_MASK        0x03
#define SDHCI_CMD_CRC              0x08
#define SDHCI_CMD_INDEX            0x10
#define SDHCI_CMD_DATA             0x20
#define SDHCI_CMD_ABORTCMD         0xC0
#define SDHCI_MAKE_CMD(_c, _f)     ((UInt16)(((_c) << 8) | (_f)))

/* TRANSFER_MODE */
#define SDHCI_TRNS_DMA             0x01
#define SDHCI_TRNS_BLK_CNT_EN      0x02
#define SDHCI_TRNS_AUTO_CMD12      0x04
#define SDHCI_TRNS_AUTO_CMD23      0x08
#define SDHCI_TRNS_READ            0x10
#define SDHCI_TRNS_MULTI           0x20

/* ---- eMMC 命令 ---- */
#define MMC_GO_IDLE_STATE          0
#define MMC_SEND_OP_COND           1
#define MMC_ALL_SEND_CID           2
#define MMC_SET_RELATIVE_ADDR      3
#define MMC_SWITCH                 6
#define MMC_SELECT_CARD            7
#define MMC_SEND_EXT_CSD           8
#define MMC_SEND_CSD               9
#define MMC_SEND_CID               10
#define MMC_READ_SINGLE_BLOCK      17
#define MMC_READ_MULTIPLE_BLOCK    18
#define MMC_WRITE_BLOCK            24
#define MMC_WRITE_MULTIPLE_BLOCK   25

/* ---- msm v5 私有寄存器：TODO，偏移待与 msm-4.4/sdhci-msm.c 核对 ---- */
#define MSM_VENDOR_CAPS            (0x000)  /* TODO */
#define MSM_VENDOR_MCI_CTRL        (0x078)  /* TODO: HC_MODE / 1.8V / DDR 控制位 */
#define MSM_VENDOR_MCI_CTRL2       (0x084)  /* TODO */
#define MSM_VENDOR_MCI_CTRL3       (0x088)  /* TODO */
#define MSM_VENDOR_DLL_CONFIG      (0x0A0)  /* TODO: DLL 锁定 */
#define MSM_VENDOR_DLL_STATUS      (0x0A4)  /* TODO */

#endif /* NATIVE_SDHCI_REGS_H */

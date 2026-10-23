# OPPO R11（r11）· SDM660 实测硬件档案

> 数据来源：真机 `fastboot getvar all` + `adb` 读 `/proc/device-tree`（本机实测），
> 以及用户提供的分区/GPT 数据。**这台机器上跑的是移植 ROM（`compatible = qcom,sdm66n-mtp`），
> 所以设备树是 MTP 参考树的改写版**：外设地址可信，panel/触摸这类板级细节要再用 stock 源码交叉验证。

## 1. 平台身份

| 项 | 实测值 |
|---|---|
| `compatible` | `qcom,sdm66n-mtp`、`qcom,sdm66n`、`qcom,mtp` |
| `model` | `Qualcomm Technologies, Inc. SDM 660 PM660 + PM660A MTP` |
| `qcom,msm-id` | `0x13d`（317）— 与 `ro.boot.product.hardware.sku=317` 一致 |
| `qcom,board-id` | `0x00000008`、`0x00003eb3` |
| `qcom,pmic-id` | `0x0001001b/0x0001011a` 等三组 |
| PSCI | `method = "smc"` ✅（8 核必需） |
| 屏幕 panel | **`qcom,mdss_dsi_oppo16051samsung_s6e3fa3_1080p_cmd`**（来自 bootargs 的 `mdss_mdp.panel=`）—— Samsung S6E3FA3 AMOLED，1080p，command 模式，single DSI |

## 2. 内存

`/proc/device-tree/memory/reg`（big-endian，2 地址单元 + 2 长度单元）：

| 段 | 基址 | 长度 |
|---|---|---|
| bank 0 | `0x00000000_80000000` | `0x00000000_80000000`（2 GiB） |
| bank 1 | `0x00000001_00000000` | `0x00000000_7e4c0000`（≈1.97 GiB） |

两段首尾相接（`0x80000000 + 0x80000000 = 0x100000000`），所以：

- `dram-base = 0x80000000`
- `dram-size = 0xFE4C0000`（≈3.98 GiB）
- 内核 `MemTotal` = 3,800,516 kB（≈3.62 GiB），差额是各 reserved region。

## 3. CPU（MPIDR 顺序已确认）

| CPU | implementer | part | 簇 |
|---|---|---|---|
| 0–3 | `0x51` | `0x801`（A53 类，silver） | cluster 0 |
| 4–7 | `0x51` | `0x800`（Kryo gold，A73 类） | cluster 1 |

→ ADT 的 `cpu_regs = "0,0x1,0x2,0x3,0x100,0x101,0x102,0x103"`（**不是** sheng 那种 one-hot 0/0x100/0x200…；sdm660 是 2 簇 × 4 核）。

`timebase-frequency = 19200000`（19.2 MHz XO）。

## 4. 外设地址表（从 `/proc/device-tree/soc/` 实测）

| 子系统 | 节点 | 地址 |
|---|---|---|
| eMMC | `sdhci@c0c4000` | `0x0C0C4000` |
| microSD | `sdhci@c084000` | `0x0C084000` |
| USB3 DRD | `ssusb@a800000`（DWC3） | `0x0A800000` |
| USB2 PHY | `qusb@c012000` / `qusb@c014000` | `0x0C012000` / `0x0C014000` |
| HS PHY | `hsusb@c200000` | `0x0C200000` |
| MDSS | `qcom,mdss_mdp@c900000` | `0x0C900000` |
| DSI PLL | `qcom,mdss_dsi_pll@c994400` / `@c996400` | `0x0C994400` / `0x0C996400` |
| MDSS CLK | `clock-controller@c8c0000` | `0x0C8C0000` |
| MMSS SMMU | `arm,smmu-mmss@cd00000` | `0x0CD00000` |
| GPU | `qcom,kgsl-3d0@5000000`（Adreno 512） | `0x05000000` |
| GPU SMMU | `arm,smmu-kgsl@5040000` | `0x05040000` |
| GPU CLK | `clock-controller@5065000` | `0x05065000` |
| GCC | `clock-controller@100000` | `0x00100000` |
| SPMI | `qcom,spmi@800f000` | `0x0800F000` |
| GIC | `interrupt-controller@17a00000` | `0x17A00000` |
| QTIMER | `timer@17920000` | `0x17920000` |
| BLSP1 UART（console） | `uart@c170000` | `0x0C170000`（`ttyMSM0`，`earlycon=msm_serial_dm,0xc170000`） |
| BLSP1 I2C | `i2c@c175000` / `c176000` / `c177000` / `c178000` | `0x0C175000`–`0x0C178000` |
| BLSP2 I2C | `i2c@c1b5000` / `c1b6000` / `c1b7000` / `c1b8000` | `0x0C1B5000`–`0x0C1B8000` |
| WLAN | `qcom,icnss@18800000`（WCN3990 系） | `0x18800000` |
| ADSP | `qcom,lpass@15700000` | `0x15700000` |
| CDSP | `qcom,turing@1a300000` | `0x1A300000` |
| SMEM | `qcom,smem@86000000` | `0x86000000` |
| IMEM | `qcom,msm-imem@146bf000` | `0x146BF000` |

**/arm-io 的 `ranges` 建议分两段**（覆盖全部子节点，且不含 DRAM）：

```
ranges = <0 0x00000000 0 0x0FF00000      /* GCC/SPMI/USB/GPU/sdhci/MDSS/SMMU */
          0 0x17000000 0 0x02000000>;    /* GIC / QTIMER / WLAN */
```

### 4.1 I2C 外设（从各 `i2c@*` 节点的子节点实测）

| 总线 | 地址 | 设备 | 用途 |
|---|---|---|---|
| `i2c@c175000`（BLSP1 I2C1） | **`synaptics16051@20`** | **Synaptics 触摸 IC，I2C 0x20** | 触摸（UEFI 和 XNU 都要它） |
| `i2c@c176000`（BLSP1 I2C2） | `bq27541-battery@55` | TI BQ27541 电量计（0x55） | 电池 |
| `i2c@c176000` | `pic16f_fastcg@25` / `stm8s_fastcg@26` | OPPO 快充 MCU（0x25/0x26） | 充电 |
| `i2c@c1b6000`（BLSP2 I2C2） | `ak4376@10` | AKM AK4376 DAC（0x10） | 音频 |
| `i2c@c1b6000` | `nq@28` | NXP NFC（0x28） | NFC |
| `i2c@c1b6000` | `tfa98xx@36` | NXP TFA98xx 智能功放（0x36） | 扬声器 |

→ 触摸驱动要按 **Synaptics `0x20`，挂在 `0x0C175000`** 写；UniXNU 侧拿 `docs/drivers/native-novatek-touch` 当模板（同样是 I2C HID 类）。

## 5. reserved-memory（决定 FD 落点和 splash 节点）

| 区域 | 地址 | 长度 |
|---|---|---|
| `splash_region@9d400000` | `0x9D400000` | `0x2300000`（35 MiB） |
| `removed_regions@85800000` | `0x85800000` | `0x5300000` |
| `modem_fw_region@8ac00000` | `0x8AC00000` | — |
| `adsp_fw_region@92a00000` | `0x92A00000` | — |
| `cdsp_fw_region@94c00000` | `0x94C00000` | — |
| `wlan_msa_mem@85700000` | `0x85700000` | — |

→ `[[bootimg.dtb_node]]` 的 splash：`reg = ["0x0", "0x9d400000", "0x0", "0x2300000"]`（**不是** sheng 的 `0xb8000000/0x2b00000`）。
→ FD 基址：Mu-Silicium 的 lavender 用 `0x9FC00000`，正好落在 splash（`0x9D400000+0x2300000 = 0x9F700000`）之上，R11 可先沿用，实测再调。

## 6. 存储与分区

| 项 | 实测值 |
|---|---|
| eMMC | 512-byte LBA，≈58.2 GiB |
| 分区方案 | 非 A/B，`boot` + `bootbak` 分离 |
| `boot` | `mmcblk0p21`，raw，`0x4000000`（64 MiB） |
| `bootbak` | `mmcblk0p22` |
| `system` | `mmcblk0p66`（bootargs 里 `dm="system none ro,0 1 android-verity /dev/mmcblk0p66"`） |
| `vendor` | `mmcblk0p67` |
| `persist` | `mmcblk0p5` |
| `modem` | `mmcblk0p64` |
| `userdata` | `mmcblk0p69`，LBA **10944512 – 122142686**（512 B 扇区），≈53.0 GiB |
| OPPO 专有分区 | `oppodycnvbk`、`oppostanvbk` |

### userdata → `board.toml` 的换算（关键）

`[userdata]` 用的是 `lba_bytes = 4096` 的单位：

| 值 | 计算 | 结果 |
|---|---|---|
| `first_lba` | 10944512 ÷ 8（恰好整除：`0x14E000 × 8`） | **`0x14E000`**（1368064） |
| `last_lba` | ⌊(122142686+1)/8⌋−1 = 15267834 | **`0xE8F7FA`** |

> 不要直接用 `122142686 ÷ 8`：末尾不满一块，且最后几个扇区是 GPT 备份表，越界会直接把备份表写坏。

## 7. 启动状态（两个来源不一致，已确认）

| 来源 | 状态 |
|---|---|
| `fastboot getvar` | `unlocked: yes`、`secure: yes`、product `sdm660`、max-download 512 MiB |
| `/proc/device-tree/chosen/bootargs` | `androidboot.verifiedbootstate=orange` ✅ 与 unlocked 一致 |
| `getprop` | `ro.boot.flash.locked=1`、`ro.boot.verifiedbootstate=green` ❌ 与上两者矛盾 |

**结论：设备已解锁**（orange + unlocked），可以 `fastboot flash boot` / `fastboot boot`。`getprop` 的 green/locked 是移植 ROM 的 build.prop 或某个 hook 改写的，不可信。
另外 bootargs 里有 `androidboot.selinux=permissive`、`printk.disable_uart=1`（stock ROM 关了串口打印）。

## 8. stock boot.img 解剖（OTA 包里的 `boot.img`）

脚本：[`scripts/split-appended-dt.py`](../scripts/split-appended-dt.py)、[`scripts/extract-panel.py`](../scripts/extract-panel.py)、[`scripts/inspect-bootimg.py`](../scripts/inspect-bootimg.py)

### 8.1 头部（→ `[bootimg.image]`）

| 字段 | 值 | 说明 |
|---|---|---|
| magic / header_version | `ANDROID!` / **0** | msm-4.4 老式 boot image |
| page_size | **4096** | |
| kernel_addr | **0x8000** | → `base = 0x0`、`kernel_offset = 0x8000` |
| ramdisk_addr | **0x1000000** | → `ramdisk_offset = 0x1000000` |
| tags_addr | **0x100** | → `tags_offset = 0x100`（OPPO 的怪习惯，别套别的机器的 0x1e00000） |
| second_addr | **0xf00000** | second_size = 0 |
| kernel_size | 16,075,142 B（gzip 成员 11,397,695 B → Image 37,741,344 B） | 内核 = Linux **4.4.78-perf+**（`root@CP-ubuntu-123`，2017-05-20） |
| ramdisk_size | 1,615,081 B | |
| cmdline | `console=ttyMSM0,115200,n8 earlycon=msm_serial_dm,0xc170000 … androidboot.usbcontroller=a800000.dwc3` | 未带 `mdss_mdp.panel=`（stock 由 dtsi 默认值选） |

mkbootimg 的地址模型是 `base + offset`，且**要求追加 DTB**；stock 镜像正是「gzip(Image) + 13 个连续 DTB」，与 Mu-Silicium 的 `Resources/Scripts/mkbootimg.py` 行为一致。

### 8.2 追加的 13 个 DTB

| # | board-id | 结果 |
|---|---|---|
| 00–04、06–12 | `0x8 / 0x0 / 0x47xx…` | 各类 sdm660 MTP/QRD 变体 |
| **05** | **`0x8 / 0x0 / 0x3eb3`** | **= 真机读到的 `qcom,board-id`，R11 就是它** |

`model` 全是 `Qualcomm Technologies, Inc. SDM 660 PM660 + PM660A MTP`，`compatible` 为 `qcom,sdm660-mtp qcom,sdm660 qcom,mtp`（注意这里是 **sdm660**，而运行中的移植 ROM 是 **sdm66n**——移植作者改过）。

### 8.3 R11 面板参数（来自 dtb-05）

| 项 | 值 |
|---|---|
| 节点 | `soc/qcom,mdss_mdp@c900000/qcom,mdss_dsi_oppo16051samsung_s6e3fa3_1080p_cmd` |
| 型号 | SAMSUNG **S6E3FA3**，`dsi_cmd_mode`，`display_1` |
| 分辨率 | 1080 × 1920（`0x438 × 0x780`），bpp 24，60 fps |
| Porch | hfp 118 / hbp 70 / hpw 16 / hskew 0；vbp 4 / vfp 20 / vpw 2 |
| 时序 | `panel-timings = 0xf03d3f00 0x6a8f3f3e 0x36030400`；`t-clk-post 0x20`、`t-clk-pre 0x3e` |
| PHY v2 | `0x241f0809 0x050304a0`（4 组重复）+ `0x241a0809 0x050304a0` |
| 通道 | `lane_map_0123`、4 lane、`burst_mode`、`te-pin-select 1`（TE 引脚）、`te-dcs-command 1` |
| 复位序列 | `[1, 5ms] [0, 2ms] [1, 12ms]`；`init-delay 5000us` |
| 背光 | `bl_ctrl_dcs`（**DCS 亮度，不走 PMIC**）；min 1 / max 255 |
| 方向 | `panel-orientation 180` |
| 自检 | `reg_read`：读 `0x0a`，期望 `0x9c` |

**on-command：22 个包 / 68 字节**（完整表见 `inputs/panel/panel-oppo16051.json`）：

```
00 05 w5   11 00            sleep out
01 39 w1   f0 a5 a5         manufacturer unlock
02 39 w1   fc 5a 5a
03 39 w1   b0 1c
04 39 w1   b5 34
05 39 w120 fc a5 a5
06 39 w1   f0 5a 5a
07-17      c3/b0 成对写（gamma/电压，末包 22 字节）
18 15 w1   35 00            set tear on
19 15 w1   53 20            write CTRL display（亮度 0x20）
20 15 w10  51 00            write display brightness
21 05 w1   29 00            display on
```

同一 DTB 里还有 3 个同族 panel（`oppo16118samsung_s6e3fa3`、`oppo17011/17021samsung_sofeg01_s`）——R11 用 **16051**。

## 9. 还没拿到的（M0 剩余项）

| 项 | 取法 |
|---|---|
| Data 卷 UUID | 首次构建后回填 `CONFIG_XNU_OWNER_DATA_VOLUME_UUID` 与 `root_match_volume_uuid` |
| 触摸固件 | Synaptics `synaptics16051` 的 fw 镜像（UEFI 阶段先只做原始坐标上报，不刷 fw） |
| ABL 的 FD 加载地址 | 解包 `boot` 的 `kernel_addr` 已确认是相对量（base 0）；FD 基址在 `0x9F700000` 以上找，`fastboot boot` 实测 |

已解决：
- **触摸 IC = Synaptics @ I2C `0x20`（`i2c@c175000`）**，见 §4.1；
- **panel 初始化序列 + 时序**，见 §8.3（`inputs/panel/panel-oppo16051.json`）；
- **`[bootimg.image]` 头部参数**，见 §8.1。

## 10. eMMC / SDHCI 实测参数（给 `NativeSdhci` 用）

设备树 `soc/sdhci@c0c4000`（eMMC）+ `dmesg`：

| 项 | 值 |
|---|---|
| compatible | `qcom,sdhci-msm-v5` |
| reg | `0xC0C4000` len `0x1000`（core）+ **`0xC0C5000` len `0x1000`（vendor/msm 专用寄存器）** |
| 中断 | `0` → **110（0x6E）**；`0` → **112（0x70）**（第二路是 vendor 中断） |
| clock-names | `iface_clk`、`core_clk`、`ice_clk`、`ice_core_clk` |
| 总线宽度 / 电压 | **8-bit**，VCC `2.95 V`（`0x2d0370` µV）、VCCQ `1.8 V`（`0x1b7740` µV） |
| 支持的速率模式 | `HS400_1p8v`、`HS200_1p8v`、`DDR_1p8v` |
| 时钟档位 | 400k / 20M / 25M / 50M / 100M / **192M** / **384M** |
| ICE 时钟 | 300 MHz / 75 MHz（`qcom_ice c0c8000.sdcc1ice`，QC ICE 3.0.72） |
| 属性 | `nonremovable`、`large-address-bus`、`sdhc-msm-crypto = 0xda` |

dmesg 关键行：

```
mmc0: SDHCI controller on c0c4000.sdhci [...] using 64-bit ADMA in CMDQ mode
mmc0: eMMC FW version: 0x15 ; CMDQ supported: depth: 16
mmc0: new HS400 Enhanced strobe MMC card at address 0001
cmdq_host_alloc_tdl: desc_size: 1024 data_sz: 63488 slot-sz: 32
sdhci_msm c0c4000.sdhci: No vmmc regulator found   ← 供电常开（不需要管）
```

→ 写驱动时的最小可用路径：**PIO/ADMA2 + 单 block 读写 + HS200/HS400 时序**，先不管 CMDQ/ICE（`CMDQ depth 16` 和 ICE 可以在挂上根卷之后再加）。

microSD（`sdhci@c084000`）：`qcom,sdhci-msm-v5`，reg `0xC084000` len `0x1000`，中断 1/2，4-bit，`SDR12/SDR25/SDR50/DDR50/SDR104`，`cd-gpios = <0x7d 0x36 0>`（检测脚）。

## 11. splash / cont-splash（阶段 1 能不能"继承"画面的依据）

| 证据 | 内容 |
|---|---|
| dmesg | `Reserved memory: reserved region for node 'splash_region@9d400000': base 0x9d400000, size 35 MiB` |
| panel 节点 | 带 **`qcom,cont-splash-enabled`**（布尔属性）= 允许 bootloader 保持面板点亮并把 framebuffer 交给内核 |
| `LOGO` 分区（p60，16 MiB） | 头 8 字节 = **`SPLASH!!`**，紧随 LE 的 `1080` × `1920`，内部还有 `oppo_1080_1920_result.raw`（16 MiB 帧）——ABL 开机点亮的就是它 |
| 35 MiB 余量 | 1080×1920×4 ≈ 8.3 MiB/帧，够 4 帧（三重缓冲 + 预留） |

**结论**：R11 与 lavender 一样走 **cont-splash**——ABL 点屏、UEFI 只把 `Display Reserved` 区包成 GOP，所以：

1. Mu-Silicium 的 `SimpleFbDxe` 依赖 `DeviceMemoryMapLib` 里的 `Display Reserved 0x9D400000`（r11Pkg 已继承，与 R11 实测完全一致）；
2. boot image 的 DTB 必须带 `splash_region` 节点，否则 **stock ABL 会关屏**（`board.toml` 里 `[[bootimg.dtb_node]]` 已补 `0x9d400000 + 0x2300000`）；
3. 阶段 1 的"有 framebuffer 输出"因此不依赖我们自己写 DSI 驱动——`DisplayDxe`（已从 xbl 抽出）负责点亮，`SimpleFbDxe` 负责 GOP。

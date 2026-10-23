# NativeSdhci — R11 的 eMMC 块设备驱动（骨架）

替代 sheng 上的 `NativeUfs`：sheng 是 UFS，R11 是 **eMMC 5.1（HS400 Enhanced Strobe）**，
项目里没有任何现成的 SDHCI 驱动，所以这是 R11 独有的、必须在阶段 6 之前完成的一块。

## 现状

| 能力 | 状态 |
|---|---|
| 匹配 provider（`compatible` 含 `qcom,sdhci-msm-v5`） | ✅ |
| core/vendor 两段寄存器映射（`0xC0C4000` / `0xC0C5000`） | ✅ |
| 软复位、1.8 V + 8-bit、400 kHz 启动时钟 | ✅ |
| eMMC 初始化：CMD0/1/2/3/9/7/16 | ✅ |
| 单块 PIO 读（CMD17） | ✅ 代码完整 |
| 写（CMD24） | ⛔ **故意禁用**（见下） |
| 多块读（CMD18）/ ADMA2 / CMDQ / ICE | ⏳ TODO |
| `CMD6` 切 HS200/HS400 + 升频到 192/384 MHz | ⏳ TODO（现在是默认速度） |
| 中断（IRQ 110/112） | ⏳ 现在全靠轮询 |
| `IOBlockStorageDevice` 对外发布 | ✅（同步 PIO，一次一块） |

### 为什么写路径被禁用

`writeBlocks()` 直接返回 `kIOReturnNotWritable`，`reportWriteProtection()` 也返回 `true`。
原因不是偷懒：**在写路径没被实机验证过之前，一个错误的分区偏移就能写掉 GPT**。
先用只读把根卷挂起来（`mount -r` / 只读 root），确认 `readBlocks` 的块号换算是对的，
再打开写。打开的顺序：写单个可牺牲的块（比如 `cache` 的最后一个块）→ 回读比对 → 才放开。

## 怎么接进构建

1. 设备仓库目录：`unixnu-r11/drivers/NativeSdhci/`（本地开发；对外发布时按项目惯例做成子模块）
2. `configs/r11_defconfig`：`CONFIG_XNU_NATIVE_SDHCI=y`（已写）
3. `board.toml` 的 `[kernel] archives` 加一条：
   ```toml
   "build-arm-driver.py NativeSdhci",
   ```
4. ADT 的 `/arm-io` 下需要有 `sdhci` 节点，`compatible = "qcom,sdhci-msm-v5"`，
   `reg` 两段（core + vendor），`interrupts` 110/112 —— 这是 `probe()` 的匹配依据。

## 验证顺序（对应 bring-up 阶段 6）

| 步 | 判定 |
|---|---|
| 1 | `native_sdhci_matched = 1` → 说明 ADT 的节点和 compatible 对了 |
| 2 | `native_sdhci_step` 停在 1–8 的哪一步 → 定位是 CMD0/CMD1/…/CMD16 哪条失败 |
| 3 | step = 9 → `readBlocks(0,1)` 成功。把读到的 512 字节打出来：应以 `55 AA`（MBR）或 `EFI PART` 偏移 0x200（GPT）开头 |
| 4 | 块设备 registerService 后，`IOBlockStorageDriver` 自动接上并出 `IOMedia` |
| 5 | 根卷 UUID 匹配 `root_match_volume_uuid` → 内核挂根 → `launchd` |

## 已知要补的坑

1. **容量**：CSD v2（HS400 卡一定是 v2）拿不到容量，得读 `EXT_CSD` 的 `SEC_COUNT`（偏移 212，4 字节 LE）。
   没有正确容量，`IOMedia` 会建出 0 字节的盘。
2. **vendor 寄存器**：`SdhciRegs.h` 里 `MSM_VENDOR_*` 的偏移是 TODO，必须对着
   `msm-4.4/drivers/mmc/host/sdhci-msm.c` 的偏移表抄。HS200/HS400 的 DLL 锁定、PHY 时序全靠它。
3. **时钟树**：SDCLK 的父时钟在 GCC 里（`clock-controller@100000`），升频前要先在平台层把这个分支使能；
   Mu-Silicium 侧已经有 `ClockDxe`，XNU 侧没有 CCF，得硬编码寄存器（参考 `sheng-pmgr` 的做法）。
4. **对齐**：`IOMallocAligned` 是权宜；正式版应该用 `IODMACommand`，否则 ADMA2 上限只有 64 KiB/描述符。

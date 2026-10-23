# ADT（Apple 设备树）—— 不是 dts

上一版这里的 `r11.dts` 是错的，已删除。按仓库实现：

| 项 | 事实 |
|---|---|
| 格式 | Apple 私有紧凑节点编码（`u32 property_count`、`u32 child_count`、属性名 32 字节 + 长度 + 值，4 字节对齐），**不是** FDT |
| 工具 | `dtc` / libfdt 只编 UEFI 侧的 FDT；ADT 必须由设备脚本生成 |
| 契约 | `[payload.adt] tool` 指向 `scripts/make-<device>-adt.py`，它把树写到 `Payloads/kernel.macho.adt`（内核旁边） |
| 参考实现 | `src/UniXNU-sheng/scripts/make-sheng-adt.py`（14 KB，直接复制改），和 `src/UniXNU/scripts/qemu-entry/make-dt.py`（最小完整示例） |

## 工具必须保证（docs/zh/porting/device-tree.md）

1. 合法 Apple 节点编码；
2. 每次生成新的 64 字节 `random-seed`；
3. 为 ramdisk / trust cache 输入加 `/chosen/memory-map` 占位符（`RAMDisk`、`PackedRAMDisk`、`TrustCache` 各 16 字节），否则 DarwinBoot 返回 `EFI_COMPROMISED_DATA`；
4. 给种子时写 `unique-chip-id` + `chip-id`；
5. 给 UUID 时写 `root-matching`；
6. 有 Data 种子时写 `ephemeral-storage`；
7. 给窗口覆盖值时写 `darwinboot-memory-size`。

## R11 必须提供的节点

| 节点 | R11 的值 |
|---|---|
| `/chosen` | `dram-base`/`dram-size`（4GB，扣除 modem/adsp/uefi 保留区）、`osenvironment=main`、`crypto-hash-method=sha2-384`、`random-seed`、CHRP NVRAM proxy（无持久 NVRAM 用空 proxy） |
| `/cpus/cpu0..7` | `reg` = MPIDR affinity（`0,0x100,...,0x700`；SDM660 是 4×A53 @0x0-0x300 + 4×A73 @0x400-0x700，**顺序以真机 `/proc/cpuinfo` 为准**）、`state=running`、`timebase-frequency=19200000`、`compatible="arm,cortex-a73"`/`"arm,cortex-a53"` |
| `/psci` | `compatible="arm,psci-1.0"`、`method="smc"`（8 核必需） |
| `/arm-io` | `#address-cells=2`、`#size-cells=2`、`ranges` 指向外设窗口；子节点 `intc`（GIC，v2/v3 以 stock dtsi 为准） |
| `/defaults` | `sleep-disabled=<1>`（未实现 suspend 时阻止 CPU 停驻） |
| `/options` | 平台选项 |
| `/product`、`/buttons` | MobileGestalt 身份与按键（`home-button-type`） |

## 取数来源

- 地址、中断号、GIC 版本：stock 内核 `arch/arm64/boot/dts/qcom/sdm660*.dtsi` + R11 板级 dts
- `dram-base`/`dram-size`：SMEM RAM table（UEFI 侧打印），或 stock `dmesg` 的 `Memory:`
- MPIDR：`adb shell cat /proc/cpuinfo`（看每个核的 `CPU part` 与顺序）

生成后自检：`strings Payloads/kernel.macho.adt | grep -E 'target-type|osenvironment|dram-size'`，
以及 XNU 起来后 `ioreg -p IODeviceTree` 能看到 `/cpus`、`/arm-io`、`/product`。

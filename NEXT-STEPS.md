# 下一步（按可行性排序，不是按文档顺序）

调研完成时间：本轮。依据是本机 clone 下来的真实仓库，不是文档推测：

- `src/UniXNU`（主仓，含 `docs/zh/porting/*`、`docs/zh/devices/*`）
- `src/UniXNU-sheng`（唯一官方 port，`board.toml` 是模板）
- `src/Mu-Silicium`（UEFI 基线，**已含 `Silicon/Qualcomm/SDM660Pkg`**）

---

## 当前状态（M0 硬件取证：已完成 ✅）

真机数据已到手并落成 [docs/hw-r11.md](docs/hw-r11.md)，`board.toml` 里下面这些已经是实测值，不再是 TODO：

| 字段 | 值 | 来源 |
|---|---|---|
| `[userdata] first_lba / last_lba` | `0x14E000` / `0xE8F7FA` | LBA 10944512–122142686（512B）换算到 4096B |
| `[payload] dram_size` | `0xFE4C0000` | device-tree memory：两段 bank 首尾相接 |
| `[payload.adt] cpu_regs` | `0,0x1,0x2,0x3,0x100,0x101,0x102,0x103` | `/proc/cpuinfo`：cpu0-3 part `0x801`(A53)，cpu4-7 `0x800`(gold) |
| `[[bootimg.dtb_node]] splash` | `0x9d400000` + `0x2300000` | `reserved-memory/splash_region` |
| 面板 | `qcom,mdss_dsi_oppo16051samsung_s6e3fa3_1080p_cmd` | bootargs 的 `mdss_mdp.panel=` |
| BL | **已解锁**（`unlocked: yes` + bootargs `verifiedbootstate=orange`） | `getprop` 的 green/locked 是 ROM 伪造，不可信 |

**M0 只剩 3 项**：panel 初始化序列（含 LAB/IBB 时序）、stock `boot.img` 的 header 参数、Data 卷 UUID（首次构建后回填）。
触摸 IC 已定位：**Synaptics，`i2c@c175000`（0x0C175000），地址 0x20**。

---

## 三个改变计划的发现

### A. 构建主机必须是 macOS（这是 P0，卡住一切）

`docs/zh/guide/requirements.md`：内核步骤调用带 `iphoneos` SDK 的 `xcodebuild`，`--skip-setup` 调 `hdiutil`。Windows 上**无法构建**，连 QEMU 冒烟都跑不了（QEMU 脚本本身是 python，但内核预链接要 Xcode）。

三条路，选一条：

1. 有 Mac / 黑苹果 → 直接上。
2. 没有 → 用 GitHub Actions 的 macOS runner：`docs/zh/building.md` 的 `macos-runner` 一节，主仓自带 `.github/workflows`。
3. 租 macOS 云（MacStadium / 阿里云 Mac 实例）。

Windows 这台机器仍然有用：刷机、EDL 备份、抓 GPT、打包/解包 boot.img、`fastboot boot` 试跑。

### B. R11 的 UEFI 是“改板”，不是“从零写”

Mu-Silicium 里已经有：

| 路径 | 内容 |
|---|---|
| `Silicon/Qualcomm/SDM660Pkg` | **SDM660 的 SoC 包（GCC/GPIO/SPMI/SDHCI/DWC3 等公共件）** |
| `Platforms/Xiaomi/lavenderPkg` + `Resources/Configs/lavender.conf` | Redmi Note 7（SDM660，1080×2340 IPS） |
| `Platforms/Xiaomi/nitrogenPkg` + `nitrogen.conf` | 小米 Max 3（SDM660，1080×2160） |

lavender.conf 的固件参数可以直接借：

```
TARGET_FD_BASE="0x9FC00000"
TARGET_FD_SIZE="0x00300000"
TARGET_ARCH="AARCH64"
```

→ `uefi-r11` = 复制 `lavenderPkg` → 改 5 处：`TARGET_FD_*`（R11 的 ABL 加载地址）、panel 初始化（R11 是 AMOLED 1080×1920，不是 IPS）、触摸 IC、PMIC 轨、按键/背光。SDM660 的时钟树和存储/ USB 不用动。

### C. 目标 IPSW 不用挑，直接复用 sheng 的

`sheng/board.toml` 已经把身份写死：`iPad7,1` / iPadOS `17.7.11` / `21H461` / `device_class = j120ap` / `chip_id = 0x8011` / `board_id = 12`，还给了两个 dmg 的 sha256 和 kernelcache sha256。

R11 可以直接用同一份 IPSW（ADT 身份是“伪装成 iPad7,1”，跟物理硬件无关）。**上一轮的更正**：iPadOS 17 是支持 A10X 的，所以“iOS 17 需要 A12+、R11 只能跑 16.x”是错的 —— armv8.0 跑 17 没问题，前提是这个生态里已经有 A10X 用户区。

---

## 执行顺序

### P0 · 环境基线（0.5–1 天）

```bash
# 任何一台机器：克隆（Windows 上必须加 http.sslBackend=openssl，schannel 在这里坏了）
git -c http.sslBackend=openssl clone --depth=1 https://github.com/UniXNU/UniXNU
git -c http.sslBackend=openssl clone --depth=1 https://github.com/UniXNU/UniXNU-sheng
git -c http.sslBackend=openssl clone --depth=1 https://github.com/M0Rf30/Mu-Silicium
git -c http.sslBackend=openssl clone --depth=1 https://github.com/acidanthera/OpenCorePkg   # edk2/Mu_Basecore 备用

# macOS 主机
brew install python@3.11 cmake ninja unifdef llvm mtools dtc qemu
python3 -m pip install -r UniXNU/requirements.txt 2>/dev/null || true
cd UniXNU && ./unixnu doctor      # 或 ./unixnu status，先看它缺什么
```

判定：`./unixnu` 能跑、能列出 feed 与步骤；`xcode-select -p` 指向完整 Xcode。

### P1 · QEMU 冒烟（1–2 天，不碰真机）

这是唯一能在做 R11 之前验证“菜单 → DarwinBoot → 内核入口”的方式。

```bash
python3 scripts/qemu-entry/make-dt.py --armvirt --armcpu --smp 2 \
  --owned-memory-size 0x20000000 --output build/qemu.dtb

python3 package/Scripts/stage-esp.py -o out/esp \
  package/UniXNUPkg/Test/Qemu/esp \
  --put out/qemu/package/Build/Out/UniXNU.efi=EFI/BOOT/BOOTAA64.EFI

python3 package/Scripts/qemu-run.py out/esp \
  --log out/qemu.log --screenshot out/qemu.png \
  --expect "UNIXNU: menu: shown" --expect "DARWINBOOT:"
```

判定：日志出现 `UNIXNU: menu: shown` 和 `DARWINBOOT:`，截图里有菜单。
**过不了 P1 就别碰 R11**：先分清是工具链问题还是板子问题。

### P2 · `uefi-r11`（3–7 天，里程碑 M1）

```bash
cp -r Mu-Silicium/Platforms/Xiaomi/lavenderPkg      Mu-Silicium/Platforms/OPPO/r11Pkg
cp    Mu-Silicium/Resources/Configs/lavender.conf   Mu-Silicium/Resources/Configs/r11.conf
```

改这 5 处（按顺序做，每改一处单独验证）：

1. `r11.conf`：`TARGET_FD_BASE` / `TARGET_FD_SIZE` —— 从 stock `boot.img` 反推 ABL 加载地址，参考 lavender 的 `0x9FC00000/0x300000`，R11 大概率同段但必须实测。
2. `r11Pkg/r11.dsc` `.fdf`：把 `lavender` 字样全换；组件表里去掉 lavender 专属（Novatek/IPS panel），换成 R11 的。
3. 显示：从 stock 内核 dtsi 抠 `qcom,mdss-dsi-on-command`，写 R11 的 panel 初始化。
4. 触摸：先只做 I2C 扫描确认 IC（FocalTech 0x38 / Goodix 0x5d / Synaptics 0x20）。
5. 背光 + 按键。

```bash
# 只 fastboot boot，不刷分区（BL 允许的前提下）
./unixnu build firmware --uefi ~/src/Mu-Silicium
fastboot boot out/r11/boot/boot.img
```

判定：屏幕出 UEFI 菜单 / Boot Manager，`fastboot` 里设备不复位。

### P3 · `UniXNU-r11` 设备仓库（1–2 周）

按真实 schema（见本目录 `board.toml`，已按 `sheng` 改写）：

```bash
cp -r UniXNU-sheng UniXNU-r11
cd UniXNU-r11 && rm -rf .git && git init
# 改 board.toml：把 sheng 换成 r11，UFS 换 SDHCI，dram_size 换 4GB
# 改 configs/r11_defconfig：CONFIG_XNU_NATIVE_UFS → CONFIG_XNU_NATIVE_SDHCI
```

- **ADT 不是 dts**：Apple 用自己的二进制节点编码，由 `[payload.adt] tool` 指定脚本生成。直接 `cp scripts/make-sheng-adt.py scripts/make-r11-adt.py` 再改地址/CPU 数（`adt/README.md` 有节点清单）。
- 必需节点：`/chosen`（random-seed、NVRAM proxy、dram-base/size、osenvironment、crypto-hash-method）、`/cpus`（8 个 `cpuN`，`reg` 是 MPIDR affinity）、`/arm-io`（带 `#address-cells`/`ranges` 的总线）、`/defaults`、`/options`、`/psci`（>1 CPU 必需）。
- `[userdata] first_lba/last_lba`：R11 GPT 里 `userdata` 条目的 LBA，必须从真机取（`lba_bytes = 4096`）。

判定：`./unixnu build` 全步骤过，产出 `out/r11/boot/boot.img` + payload。

### P4 · M0 取证 + 上机（1–2 周）

```powershell
# Windows 侧（本机）
adb shell 'ls -l /dev/block/by-name/'          # 分区名 → p 号
adb shell 'cat /sys/block/mmcblk0/mmcblk0p21/start /sys/block/mmcblk0/mmcblk0p21/size'
fastboot getvar all
# EDL 全量备份（先备份，再刷任何东西）
edl printgpt --loader=prog_emmc_firehose_SDM660_ddr.elf --memory=eMMC
```

然后照 `docs/BRINGUP.md` 的 S0–S8 推进。

---

## 与上一轮交付的更正（重要）

| 上一轮说的 | 实际（以仓库为准） |
|---|---|
| `board.toml` 用 `[board] soc/arch/page_size/...` 自定义段 | 真实 schema：`[board] kit`、`[ipsw] url/product/chip_id/board_id`、`[userdata] first_lba/last_lba/lba_bytes/windows`、`[kernel] defconfig/archives`、`[kernel.prelink]`、`[firmware] config/dsc/fd/preflight/packages`、`[payload] volume/size_mib/kernel_args`、`[payload.adt] tool/chip_id/...`、`[[bootimg.dtb_node]]` |
| defconfig 用 `CONFIG_PLATFORM_*` / `CONFIG_DRIVER_*` | 实际是 `CONFIG_XNU_<NAME>`，每个变成内核构建的 `XNU_<NAME>` |
| ADT 是 dts，用 `dtc` 编 | ADT 是 Apple 私有二进制编码，`dtc` 只适用于 UEFI 侧的 FDT；ADT 由 `make-<device>-adt.py` 生成 |
| 16KB 页 | UniXNU 走 **4 KiB** XNU 补丁（`docs/xnu/four-k-page-tables.md`，内核用 `mach.development.bcm2837`） |
| Adreno 512 完全没戏 | Mu-Silicium/UniXNU 有 `CONFIG_XNU_NATIVE_ADRENO` 与 `docs/drivers/native-adreno`；**sheng 已验证 GPU**，R11 的 Adreno 512 是否覆盖未知，但至少有路 |
| iOS 17 要 A12+ | iPadOS 17 支持 A10X；sheng 就是 iPad7,1 + 17.7.11 |
| 存储：UFS/APFS 先行 | 项目主路径是 **UFS**（sheng）；R11 是 eMMC，需要新写 `NativeSdhci`（`CONFIG_XNU_NATIVE_SDHCI`）——这是 R11 独有的额外工作量 |

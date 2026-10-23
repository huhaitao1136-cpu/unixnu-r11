> ## ⚠️ 本文已被 [NEXT-STEPS.md](NEXT-STEPS.md) 取代部分结论
>
> 这一版是在拿不到仓库的情况下按截图推演写的。仓库现已 clone（`../src/UniXNU`、`../src/UniXNU-sheng`、`../src/Mu-Silicium`），以下几条已确认是错的，**以 NEXT-STEPS.md 为准**：
>
> | 本文的说法 | 实际 |
> |---|---|
> | `board.toml` 的 `[board] soc/arch/page_size`、`[storage]`、`[console]` 等自定义段 | 真实 schema 见本文同目录 `board.toml`（已按 sheng 重写） |
> | defconfig 用 `CONFIG_PLATFORM_*` / `CONFIG_DRIVER_*` | 实际是 `CONFIG_XNU_<NAME>`（已按 sheng 重写 `configs/r11_defconfig`） |
> | ADT 是 dts、用 `dtc` 编 | ADT 是 Apple 私有二进制编码，由 `make-<device>-adt.py` 生成；见 `adt/README.md` |
> | 16KB 页 | UniXNU 走 4 KiB 页（`docs/xnu/four-k-page-tables.md`） |
> | iOS 17 需 A12+，只能选 16.x | iPadOS 17 支持 A10X，sheng 就是 iPad7,1 + 17.7.11，照抄即可 |
> | R11 只能软件渲染 | 项目有 `CONFIG_XNU_NATIVE_ADRENO`，sheng 已验证 GPU；R11 的 Adreno 512 未知但非死路 |
> | 从零写 UEFI | Mu-Silicium 已有 `SDM660Pkg` + `Platforms/Xiaomi/lavenderPkg`（Redmi Note 7），改板即可 |
>
> 仍然有效的部分：硬件事实表、M0 备份流程、风险表、驱动清单（除存储项——R11 是 eMMC，需要新写 `NativeSdhci`）。

# UniXNU → OPPO R11（SDM660 / `r11`）移植手册

> 目标：把 [UniXNU](https://github.com/UniXNU/UniXNU) 的 Darwin/XNU 用户区带到 OPPO R11（CPH1707，Snapdragon 660）。
> 骨架按截图里的“分工模型”搭建：**通用仓库不改 → 新开两个设备仓库（`UniXNU-r11` + `uefi-r11`）→ 用 `board.toml` + ADT 把设备描述清楚**。
>
> 说明：本机网络被拦（DNS 落到 198.18/2001:2 假地址，schannel 无凭证），GitHub 拉不下来，`C:\Users\wang\Documents\ChatGPT\UniXNU` 是个空仓（只有 `.git`，无 commit）。下面凡是涉及上游具体文件名的（`board.toml` 字段、`kusd`、Mu-Silicon 的 DSC 路径）都按“骨架/待对齐”标注，clone 下来后按实际字段改名即可，流程不变。

---

## 0. 先给结论

| 项 | 判断 |
|---|---|
| 是否“移植” | 不是。UniXNU 目前只有 `sheng`（SM8550，小米平板 6S Pro）一个公开 port，**没有 SDM660 的任何代码**。这次是从零开一个 port，等于给项目加一个 SoC 家族。 |
| 硬件够不够 | 够。arm64 v8.0、8 核（4×A73 + 4×A53）、4GB LPDDR4X、支持 16KB 页 —— XNU arm64 的基本要求都满足。 |
| 最大三个卡点 | ① 能否把自定义 UEFI 写进设备（BL/EDL）；② UEFI 阶段能不能出画面 + 出菜单（AMOLED DSI 没有简单 framebuffer）；③ **Adreno 512 没有 Metal 驱动**，只能软件渲染，且 iOS 用户区必须挑 armv8.0 还支持的版本（≤ 16.x）。 |
| 工作量 | 单人 3–6 个月到“内核打印跑起来”；1–2 人 2–3 个月到 M3（内核入口）。UI 可用是另一个量级。 |
| 成功概率 | M1（UEFI 菜单出画面）≈ 70%；M3（XNU 进 Platform Expert）≈ 40%；M5（用户区起 launchd）≈ 15%；完整可用 ≈ 5%。原因见 §12 风险表，主要是 GPU 和 APFS/SSV。 |

**顺序建议**：先做 M0 备份 + 硬件取证（不改机），再决定要不要真干。M0 的价值在于：如果 R11 的 EDL 写了禁止位或 secboot 把 boot 分区烧死了，后面所有事都不用谈。

---

## 1. 把截图里的“分工”对上具体产物

| 截图里的条目 | 落到磁盘上的东西 | 复用 / 逐设备 |
|---|---|---|
| `UniXNU-<device>`：`board.toml`、kernel defconfig、设备脚本、驱动、套件、组件、设备包 | `UniXNU-r11/`：`board.toml`、`configs/r11_defconfig`、`drivers/`、`packages/`、`scripts/`、`overrides/` | 逐设备 |
| `uefi-<device>`：Mu-Silicon 的 UEFI 移植，设备配置 / DSC / FDF / 预检 / 供应商输入 | `uefi-r11/`：`Platform/OPPO/R11Pkg/{R11Pkg.dsc,R11Pkg.fdf,R11Pkg.dec}`、`Include/`、`Library/`、`Drivers/` | 逐设备，但 90% 是抄 Mu-Silicon 的 Qualcomm 公共件 |
| “UniXNU 本体设备仓库用同名模块覆盖通用” | `overrides/` 目录：文件名与上游模块同名，构建时优先 | 机制 |
| 不用改、直接复用 | 构建系统、模块解析、XNU 源码、kext 加载器、payload 打包脚本 | 通用 |
| 必须逐设备填 | UEFI 移植 + 供应商 blob（panel 初始化序列、触摸固件、modem 固件） | 逐设备 |

“一句话：把 `board.toml` + ADT 把设备描述清楚” —— 这两份文件就是整个 port 的**唯一真相源**：构建系统只读它们，驱动只按它们 probe。

---

## 2. R11 硬件事实表（打 ✅ 的是确定的，❓ 的必须实测）

| 子系统 | 事实 | 状态 |
|---|---|---|
| SoC | Snapdragon 660 (SDM660)，Kryo 260，4×A73@2.2G + 4×A53@1.84G，armv8.0-A | ✅ |
| 中断控制器 | GIC（以 stock `sdm660.dtsi` 的 `intc` 节点为准，v2/v3 决定 XNU 侧要不要改） | ❓ |
| 内存 | 4GB LPDDR4X，DDR 基址与保留区从 XBL/dmesg 拿 | ❓ 基址 |
| 存储 | 64GB，eMMC 5.1（R11 系列同平台；R11 Plus/11s 也是） | ❓ 实测 |
| 扩展存储 | microSD（SIM2 混合卡槽）→ **最好的第一块 XNU 根卷载体** | ✅ |
| 显示 | 5.5" 1080×1920 AMOLED，Qualcomm MDSS + DSI，无简易 FB | ✅ |
| 触摸 | FocalTech / Goodix / Synaptics 三者之一（看 stock defconfig 的 `CONFIG_TOUCHSCREEN_*`） | ❓ |
| USB | micro-USB 2.0，DWC3 + QUSB2 PHY | ✅ |
| PMIC | PM660 + PM660L，SPMI | ✅ |
| Wi-Fi/BT | WCN3990 系（SNOC），后期再说 | ❓ |
| 调试串口 | BLSP1 UART 未引出 → **UEFI/XNU 早期日志只能走 FrameBuffer 或 USB CDC** | ✅ |
| BL | Qualcomm XBL/ABL，secure boot 开；OPPO 无官方解锁通道 | ✅ |
| Stock 内核 | ColorOS，Linux 4.4（msm-4.4），OPPO 开源包 / 社区镜像 `color597/android_kernel_oppo_sdm660-9` | ✅ |

> 这张表里每一个 ❓ 都要在 M0 用一条命令填掉，别靠猜 —— ADT 里错一个基址就是三天白干。

---

## 3. M0：备份 + 取证（不改机，必须做完）

### 3.1 拿 stock 内核源码，把硬件参数挖出来

```bash
# 社区镜像（OPPO sdm660 开源包）
git clone --depth=1 https://github.com/color597/android_kernel_oppo_sdm660-9
cd android_kernel_oppo_sdm660-9

# 1) 屏幕：panel 节点 + 初始化序列（后面 UEFI DisplayDxe 直接吃这张表）
grep -rn "qcom,mdss-dsi-panel-name" arch/arm64/boot/dts/qcom/ | head
grep -rn "qcom,mdss-dsi-on-command"    arch/arm64/boot/dts/qcom/ | head

# 2) 触摸
grep -rn "compatible.*\(focaltech\|goodix\|synaptics\|ft8716\|gt9\)" arch/arm64/boot/dts/qcom/ | head

# 3) 存储 / USB / 中断 / DDR 基址
grep -n "sdhc_1\|sdhc_2" arch/arm64/boot/dts/qcom/sdm660*.dtsi | head
grep -n "dwc3\|qusb2"    arch/arm64/boot/dts/qcom/sdm660*.dtsi | head
grep -n "intc:" -A6       arch/arm64/boot/dts/qcom/sdm660*.dtsi
grep -n "memory@\|reg = <0x0 0x80000000" arch/arm64/boot/dts/qcom/sdm660*.dtsi | head

# 4) 整机 dts（OPPO 的板级文件，名字里带 r11）
ls arch/arm64/boot/dts/qcom/ | grep -i "r11\|oppo\|1707"
```

Windows 上用 `scripts/probe-hw.ps1 -KernelSrc <path>` 一次跑完并输出表格。

### 3.2 真机取证（只读，安全）

```bash
fastboot getvar all                     # BL 状态、slot、secureboot、分区列表
fastboot oem device-info                # 有些 ABL 会回 unlocked 字段
adb shell 'cat /proc/partitions; cat /proc/iomem | head -40'
adb shell dmesg | grep -iE "memory|panel|touch|sdhci|dwc3|gcc|spmi"
adb shell 'cat /sys/class/backlight/*/max_brightness'   # panel 亮度通道
```

### 3.3 EDL 全量备份（唯一救砖手段，务必先做）

SDM660 用 `prog_emmc_firehose_SDM660_ddr.elf`（OPPO 签名版可从官方固件包里抠），写入路径靠 `rawprogram*.xml`。

```bash
# Linux 上最省事：bkerler/edl
pip install edlclient
edl printgpt --loader=prog_emmc_firehose_SDM660_ddr.elf --memory=eMMC
edl rf full_backup.bin --loader=prog_emmc_firehose_SDM660_ddr.elf --memory=eMMC
# 至少单独备份：gpt/gpt_backup、xbl、abl、tz、hyp、devcfg、keymaster、modemst1/2、fsg、persist、frp、devinfo
```

Windows 用 QFIL 的 “Flat Build + Readback” 做同样的事。**备份完先验证能把 `boot` 原样刷回去、能开机**，再动第 4 步。

### 3.4 M0 的 go / no-go

- EDL 能读能写、备份可回刷 → 继续。
- `fastboot flashing unlock` 不可用但 EDL 可写 → **可行**，走 EDL 直接写 `boot` 分区（PBL 的 firehose 是签名但不校验目标分区内容，这是刷自定义 UEFI 的正路）。
- EDL 需要 auth 且没有 OPPO programmer → 停，去搞 programmer，别硬上。

---

## 4. 步骤 1：UEFI 固件（最重的一步，决定生死）

### 4.1 基线

用 [Mu-Silicon](https://github.com/M0Rf30/Mu-Silicium)（Project Mu 的 Qualcomm 移植，社区有 `Azkali/MU-Qcom`、`Albert753258/Mu-Qcom` 等 fork）当骨架，不要从 EDK2 裸写。

```
uefi-r11/
├── Platform/OPPO/R11Pkg/
│   ├── R11Pkg.dsc              # 组件清单 + PCD
│   ├── R11Pkg.fdf              # flash 布局（FD/FV 大小、DXE 顺序）
│   ├── R11Pkg.dec
│   ├── Include/                # 板级 PCD、panel 表、GPIO 表
│   ├── Library/
│   │   ├── R11PlatformLib/     # 时钟、GPIO、PMIC 初值
│   │   └── R11DisplayLib/      # DSI panel init（从 §3.1 的表生成）
│   └── Drivers/
│       ├── R11DisplayDxe/      # MDSS+DSI → GOP
│       ├── R11SdhciDxe/        # eMMC + microSD → BlockIo
│       ├── R11UsbDxe/          # DWC3 → XHCI（Mu 有公共件，改 PHY/glue）
│       └── R11PmicDxe/         # PM660/660L 电压轨
└── Silicon/Qualcomm/SDM660/    # 若上游无此 SoC 包，从 MSM8998/SM8250 包改
```

### 4.2 必做的 6 件事

1. **FDF 布局**：`boot` 分区（R11 一般 64MB）装 FD；`PcdFdBaseAddress` 用 ABL 加载地址（从 stock `boot` 镜像的 `kernel` 偏移反推，通常 0x80000000 附近 + 偏移）。
2. **显示**：MDSS 的 DSI PHY/PLL 参数 + panel `on-command` 表，直接从 §3.1 抠；先点亮 1080×1920@60 的 linear framebuffer，GOP 出 1024×768 letterbox 就够菜单用。
3. **日志**：`DebugLib` 双后端 —— 早期写 FrameBuffer 的固定区域（自己画 8×16 字库），起来后切 USB CDC-ACM（`UsbSerialDxe`）。**没有这一步会盲调**。
4. **存储**：SDHCI 先只做 microSD（简单、无 RPMB），能读 FAT32 的 ESP 就够 `BdsDxe` 加载 payload；eMMC 随后补。
5. **时钟/电源**：GCC 的 UART/SDCC/USB/DSI 分支时钟必须由 `R11PlatformLib` 显式使能 —— Qualcomm 平台 90% 的“卡在 DxeCore 之后没输出”都是时钟没开。
6. **菜单**：`BdsDxe` + `UiApp` 出 Boot Manager；加一个自定义 `R11SetupDxe`，菜单项 = { Boot UniXNU, Boot Android, Dump ADT, UEFI Shell }。

### 4.3 判定（M1）

能看到 Boot Manager 菜单、能列出 SD 卡里的文件、键盘/触摸至少一种能操作 → M1 通过，拍照存档。

---

## 5. 步骤 2：建设备仓库 `UniXNU-r11`

```
UniXNU-r11/
├── board.toml                     # 唯一真相源（见下）
├── configs/r11_defconfig          # 从上游 sheng_defconfig 复制，只改 SoC/驱动选择
├── adt/
│   ├── r11.dts                    # ADT 源（从参考 IPSW 的 DeviceTree 反编译后改）
│   └── build-adt.sh               # dts → ADT（含 phandle 重编号）
├── drivers/
│   ├── sdm660-sdhci/  dwc3-usb/  mdss-dsi-r11/  fts-touch/  pm660-power/  gcc-sdm660/
├── packages/
│   ├── r11-firmware/              # panel/touch/modem 固件（仅自用，注意许可）
│   └── r11-userland-overlay/      # 替换掉用不到的 kext、补 launchd 配置
├── overrides/                     # 与上游同名即覆盖
├── payload/{boot.cfg, kernel.args}
├── scripts/{pack-payload.sh, flash-boot.sh, edl-backup.sh, probe-hw.ps1}
└── docs/BRINGUP.md                # 阶段清单（本目录 docs/BRINGUP.md）
```

### 5.1 `board.toml` 要点

见同目录 `board.toml`（骨架，字段名与上游对齐后改名）。核心字段族：

- `[board]`：`soc / arch / page_size / cpus / ram / ddr_base` —— 构建期用来选 defconfig 和链接地址。
- `[uefi]`：固件仓路径、payload 所在分区、FD 基址。
- `[ipsw]`：参考设备 + iOS 版本 + 解包产物校验和（步骤 3 的产物锁版本）。
- `[adt]`：dts 源、生成命令。
- `[storage]`：根卷设备（先 microSD，后 eMMC）、分区名、文件系统。
- `[console]`：uart / usb / fb 三选，早期必须是 fb。
- `[boot]`：`boot-args`（`-v keepsyms=1 debug=0x144 serial=1`）。

### 5.2 defconfig

从上游 `sheng` 的 defconfig 出发，只动这几处：

```
CONFIG_ARCH_ARM64=y
CONFIG_PLATFORM_SDM660=y        # 新增（上游叫 PLATFORM_<SOC>）
CONFIG_SOC_SDM660=y
CONFIG_DRIVER_SDHCI_MSM=y
CONFIG_DRIVER_DWC3=y
CONFIG_DRIVER_MDSS_DSI=y
CONFIG_DRIVER_PM660=y
CONFIG_DRIVER_FTS_TOUCH=y
# CONFIG_DRIVER_ADRENO is not set      ← 没有 GPU 驱动
CONFIG_SERIAL_FB_CONSOLE=y
CONFIG_KDTRACE=n
CONFIG_DEBUG_KERNEL=y
```

> `kusd` 类的内核/用户区描述文件（截图里那个标签页）属于 **KernelSU 体系**，跟本 port 无关 —— 工作区里 `GhostSam_src/`、`ksu_src/`、`ksud_patch.py` 是上一次在改 KernelSU 的 ksud，别混进来。

---

## 6. 步骤 3：锁定目标 IPSW（这里最容易选错）

**不要选 iOS 17。** iOS 17 起只支持 A12+，用户区普遍用 armv8.3 的 PAC/BTI 指令；SDM660 是 **armv8.0**（A73/A53），遇到 `PACIA`/`BTI` 直接非法指令。

推荐组合：

| 参考设备 | 型号 ID | RAM | iOS 上限 | 选择理由 |
|---|---|---|---|---|
| iPhone 8 | `iPhone10,1` | 2GB | 16.7.x | armv8.0 + 16KB 页，用户区最“朴素”，跟本机同代 |
| iPad (第 6 代) | `iPad7,5` | 2GB | 17.x 但取 16.7.x | A10，屏幕分辨率接近（2048×1536 vs 1080×1920，缩放好做） |

拿一个 16.7.x 的 IPSW，解包需要的东西：

```bash
unzip -o iPhone_4.7_16.7.10_*.ipsw -d ipsw/
# 关键件
ls ipsw/Firmware/all_flash/ | grep DeviceTree        # ADT 源 → 步骤 4
ls ipsw/Firmware/ | grep -E 'kernelcache|^dfu'       # XNU 参考映像（对照补丁点）
# 用户区
dmg2img ipsw/*.dmg 或 hdiutil attach                  # 取 System/、usr/lib/、bin/、sbin/、Library/
ls ipsw/ | grep -i basebin                            # iOS 15+ 的 launchd 在 basebin 里
```

必做的补丁面（这一步是“能不能进用户区”的分水岭）：

1. **AMFI / 代码签名**：内核侧关 `cs_enforcement`、`amfi_get_out_of_my_way` 等旗标，或直接不打 AMFI kext。
2. **KPP/KTRR**（A10 有 KPP）：要么打 KPP bypass（老设备那套），要么只在“不触发 KPP 检查点”的路径上跑。
3. **SEP 缺失**：`AppleSEPManager` 相关 kext 全部不加载，凡是走 SEP 的（keybag、passcode、部分 crypto）全部 stub。
4. **设备型号伪装**：`images.plist`、`IODeviceTree:/product` 的 `target-type`/`product-name` 要和 ADT 一致，否则 `launchd` 中途退出。
5. **OTA/SSV**：iOS 16 的 root 卷是 APFS 快照 + 签名卷组，先别碰 SSV，直接把系统装成普通 APFS 卷并关掉验证。

---

## 7. 步骤 4：ADT（Apple Device Tree）

### 7.1 做法

1. 从参考 IPSW 取 `DeviceTree.<board>.im4p` → `img4tool`/`pzb` 解出 lzss 后的 ADT → `adt2dts` 转成文本 dts。
2. 用它当模板，只改这几块（见 `adt/r11.dts`）：
   - `/memory`：DDR 基址 + 大小（4GB 但要扣掉 modem/adsp/uefi 保留区）。
   - `/cpus/cpu@0..7`：`state`、`timebase-frequency`（SDM660 的 arch timer 是 19.2MHz XO）。
   - `/arm-io`：`reg`/`ranges` 把 GIC、UART、SDHCI、DWC3、DSI 的物理地址搬进来 —— XNU 的 arm-io 驱动按这里的 reg 做 `IODMACommand` 映射。
   - `/arm-io/gic`、`/arm-io/timer`：中断与定时器（XNU 不认 Linux 的 `interrupt-parent` 语义，要显式节点）。
   - `/product`：`product-name` / `target-type`（伪装成参考设备）。
   - `/chosen`：`boot-args`、`display-config`（1080×1920 的时序，给 IOMobileFramebuffer 用）。
3. 生成时**必须重编号 `AAPL,phandle`**（Apple ADT 是 big-endian、phandle 全表一致），`build-adt.sh` 里用 `dtc -I dts -O dtb` 后再跑一遍 phandle 修正。

### 7.2 判定

`adt2dts` 反向能读回、`strings` 里能看到 `target-type`，且 XNU 启动时 `IODeviceTree` 能列出全部节点（`ioreg -p IODeviceTree` 阶段）。

---

## 8. 步骤 5：有效载荷 + 启动映像

两个产物，别混：

| 产物 | 内容 | 放哪 |
|---|---|---|
| `payload`（UEFI 直接加载） | `mach_kernel`（arm64 Mach-O）+ `boot.cfg` + kext 目录 + ADT blob | ESP：`\EFI\UniXNU\r11\` |
| `boot.img`（Android 侧兜底） | 上面 payload 打成 Android boot image v2（`kernel`=loader stub，`ramdisk`=payload） | `boot` 分区 |

启动链：

```
PBL → XBL → ABL → [boot 分区里的 uefi-r11 FD]
                        │
                        ├─ BdsDxe 菜单
                        └─ UniXNU loader：读 ESP → 载入 ADT + mach_kernel + boot.cfg
                                └─ ExitBootServices → 跳 XNU entry (arm64, 16K 页)
```

`boot.cfg` 里先只写最小集：

```
-V
keep-syms
boot-args = -v keepsyms=1 debug=0x144 serial=1 io=0xffffffff
adt = r11.adt
kernel = mach_kernel
kexts = /kexts
```

---

## 9. 步骤 6：分阶段点亮（照抄截图那 8 个阶段，加判定信号）

| # | 阶段 | 判定信号 | 90% 的死因 | 排查 |
|---|---|---|---|---|
| 1 | 固件（UEFI 起） | 屏幕出 Boot Manager | FDF 装错地址 / GCC 时钟没开 / panel init 序列错 | FrameBuffer 早期日志；换 1024×768 试试 |
| 2 | 菜单 | 能选、能读 SD 卡 | SDHCI 时钟/电压（1.8V vs 3.3V） | `ls` UEFI Shell |
| 3 | 有效载荷 | loader 打印 “ADT ok / kernel ok” | FAT 路径 / 大小校验 / 内存不够 | 打印每段 CRC |
| 4 | 内核入口 | XNU 第一行 `AppleARMPE`/`Darwin Kernel Version` | entry 地址、页大小、MMU 页表 | 先关 SMP 单核起 |
| 5 | Platform Expert | `IODeviceTree` + `AppleARMPE` 注册成功 | ADT 缺节点 / phandle 乱 | `-v` 看 `PE_init_platform` 停在哪 |
| 6 | 存储 | 能看到 block device | SHDCI 中断不走 GIC / DMA 掩码 | 改用 polling 模式 |
| 7 | 根 | `mount root` 成功（先 HFS+，后 APFS） | 卷头/快照/大小端 | 用 microSD + HFS+ 先过 |
| 8 | 用户区 | `launchd` 起来、有 `WindowServer` 或至少 `getty` | AMFI / SEP / basebin / 型号伪装 | 用参考机型的 `launchd` 加 `-x` 安全模式 |

每一阶段都**只动一个变量**，并且把 UEFI 阶段的日志落到 SD 卡文件里（`R11DebugDxe` 每 4KB 刷一次），不然一挂就什么都没有。

---

## 10. 步骤 7：驱动清单（SDM660 逐项）

| 子系统 | Linux/stock 参考 | XNU 侧要写什么 | 难度 |
|---|---|---|---|
| eMMC / SD | `sdhci-msm`, `sdhci` | `IOBlockStorageDevice` + `IOBlockStorageDriver` + `IOMedia`（先 PIO，后 ADMA） | ★★★ |
| USB | `dwc3`, `xhci` | UEFI 侧出 XHCI → XNU 侧主机控制器栈（AppleUSB 系没有 ARM 版 glue，要自己写 `IOUSBHostController`） | ★★★★★ |
| 显示 | `mdss`, `dsi`, panel | ①先把 UEFI GOP 的 FB 做成 `IOMobileFramebuffer` 类；②再做 DSI 原生驱动 | ★★★ |
| 触摸 | `fts_ts` / `goodix` / `synaptics` | `IOHIDEventService` + I2C 控制器（BLSP I2C） | ★★ |
| 电源 | `qcom-spmi-pmic`, `qpnp-*` | SPMI 控制器 + `AppleSmartBattery` 桩（先假电池） | ★★★ |
| 时钟 | `gcc-sdm660` | 平台 expert 里的 clock gate 表（XNU 无 CCF，硬编码寄存器） | ★★ |
| 中断 | `arm,gic-v3` | GIC 初始化 + `AppleARMIO` 中断分发 | ★★★ |
| remoteproc | `adsp`, `ssc`, `aop` | XNU 的 `AppleA7IOP` 类比；早期全部不加载 | ★★★★ |
| GPU | Adreno 512（freedreno a5xx 有参考） | **无 Metal 驱动**：只能 CoreGraphics 软件渲染 + FB blit | ★★★★★（先不做） |
| Wi-Fi/BT | `wcn3990` | 后期；早期用 USB 网卡或不要网 | ★★★★ |

**结论：先用 microSD + software FB + 触摸，把 1→8 打通；eMMC/UFS、GPU、Wi-Fi 全部推后。**

---

## 11. 步骤 8：用户数据镜像

1. **分区规划**（保持 Android 可回退）：从 `userdata` 尾部切一块 `darwin-data`（GPT 新增条目，`sgdisk -n`，注意 `-e` 扩表 + 备份表同步）。
2. **根卷格式**：先用 **HFS+**（XNU 原生、无 SSV），镜像用 `newfs_hfs` 或 macOS 上 `hdiutil create -fs HFS+`；APFS 留到 M6。
3. **打包**：把 IPSW 解出的 `System/`、`usr/`、`bin/`、`sbin/`、`Library/`、`basebin/` 拷进根卷，删 kext 目录里所有走 SEP/GPU 的项。
4. **刷写**：`dd` 到 `darwin-data`，或 UEFI 里做一个 “Dump FS” 项直接写块设备。
5. **首启**：`boot-args` 加 `-s`（单用户）先拿 root shell，再试 `launchd`。

---

## 12. 里程碑与风险

| 里程碑 | 验收 | 预计 |
|---|---|---|
| M0 备份 + 硬件表填满 | EDL 可回刷、硬件表无 ❓ | 1–2 周 |
| M1 UEFI 出菜单 | 屏幕有菜单、能读 SD | 4–8 周 |
| M2 payload 加载 | loader 打印全绿 | 2 周 |
| M3 XNU 进 PE | 内核打印 + ioreg 有节点 | 4–6 周 |
| M4 根卷挂载 | 单用户 shell | 3–4 周 |
| M5 launchd | 有进程树 | 4–8 周 |
| M6 可用 UI | 软件渲染、触摸可用 | 无上限 |

风险表：

| 风险 | 影响 | 缓解 |
|---|---|---|
| BL 锁死 / EDL 需 auth | 直接终止 | M0 先验；找 OPPO 签名 programmer |
| AMOLED DSI 不出画面 | 盲调 | 先用 UEFI 的简单 FB；外部 HDMI 不存在就靠 USB CDC 日志 |
| Adreno 512 无 Metal | UI 只能软件渲染 | 接受；或移植 freedreno 思路做最小 2D |
| iOS 用户区与硬件代差 | launchd 秒退 | 锁 iOS 16.7.x，型号伪装成 iPhone10,1 |
| APFS/SSV | 根卷挂不上 | 先 HFS+ |
| 上游还在 WIP | 接口随时变 | 锁上游 commit，`overrides/` 兜底 |

---

## 13. 参考

- [UniXNU/UniXNU](https://github.com/UniXNU/UniXNU) —— 主仓（构建系统 + XNU 补丁）
- [UniXNU/UniXNU-sheng](https://github.com/UniXNU/UniXNU-sheng) —— 唯一公开设备 port（SM8550），**照它的目录结构抄**
- [Mu-Silicon](https://github.com/M0Rf30/Mu-Silicium) / [MU-Qcom](https://github.com/Azkali/MU-Qcom) —— UEFI 基线
- [color597/android_kernel_oppo_sdm660-9](https://relatedrepos.com/gh/color597/android_kernel_oppo_sdm660-9) —— SDM660 内核/DTS 参考
- [Linux `sdm660.dtsi`](https://android-kvm.googlesource.com/linux/+/05aa0eb325c98f7e06eeb3f251cbab1b27586c8e/arch/arm64/boot/dts/qcom/sdm660.dtsi) —— 地址/中断的权威表
- 报道：[UniXNU：安卓刷 iOS 通用框架](https://xiaoyi.vc/unixnu.html)、[小米平板 6S Pro 跑 iPadOS 17](https://www.vgover.com/news/241755)

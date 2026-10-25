# R11 移植进度（阶段 1 完成）

> 最后更新：2026-10-23 15:40
> 设备：OPPO R11 (CPH1707)，SDM660，4 GiB，1080×1920

## 一、当前状态

**阶段 1 已完成**：自制的 UEFI 固件能加载、能初始化 DXE、能跑到 BDS（启动管理器），屏幕有完整调试输出。

BDS 最后一行：

```
Boot Loader failed: efi no media
Error: Failed to GetBootLoaderInfo
```

含义：**没有存储驱动**，BDS 找不到可启动的分区。不是崩溃，是"跑到底但没东西可加载"。

## 二、这次真正解决的问题（工具链可信度）

之前十几轮"刷了没反应"全是假象，根因有三个：

| 环节 | 曾经的坑 | 现在的做法 |
|---|---|---|
| 构建 | 增量缓存导致 `.inc`/`BootShim.S` 改动没进 FD | `rm -rf Build/r11Pkg` 全量重编，打印 sha256 |
| 刷写 | `fastboot flash boot` 返回 `OKAY` 但**实际没写**，后来干脆 `FAILED (unknown command)` | **TWRP + dd**，写完回读 md5 逐字节核对 |
| 判断 | 拿旧画面当新结果（cont-splash 保留了上次的文字） | 先解包镜像确认内容，再刷 |

### 可靠的刷写流程（唯一可信）

```powershell
# 进 TWRP：长按电源 20s → 音量上+电源
adb push E:\DeepSeekWorkspace\out\r11\boot\boot.img /tmp/boot.img
adb shell "dd if=/tmp/boot.img of=/dev/block/bootdevice/by-name/boot bs=4096"
adb shell "dd if=/dev/block/bootdevice/by-name/boot bs=4096 count=367 2>/dev/null | md5sum"
adb reboot
```

回读 md5 必须与本地文件一致。**回滚**：

```powershell
adb push E:\DeepSeekWorkspace\R11-backup\firmware\boot.img /tmp/stock.img
adb shell "dd if=/tmp/stock.img of=/dev/block/bootdevice/by-name/boot bs=1M"
adb reboot
```

## 三、关键构建/运行参数

| 项目 | 值 |
|---|---|
| FD 基址 / 大小 | `0x9FC00000` / `0x300000` |
| BootShim | `_Start` 前插入宏映射，`image_size = UEFI_SIZE + (_Payload - _Head)` |
| 显示 | 移除厂商 `DisplayDxe`，由 `SimpleFbDxe` 独占 GOP，`MipiFrameBuffer 1080×1920×32` |
| 串口/控制台 | 无串口，靠屏幕 |
| 调试等级 | 默认全开（含内存映射）；排查时可用 `PcdDebugPrintErrorLevel\|0x80000000` 只看错误 |
| 构建命令 | `SKIP_MUPATCH=1 R11_FB_MARK=0 bash scripts/rebuild-clean.sh` |

## 四、Mu 源码补丁（`scripts/patch-mu.sh`，跑完三行验证都必须是 1）

| # | 文件 | 改动 | 目的 |
|---|---|---|---|
| 1 | `Mu_Tiano/ArmPkg/Drivers/TimerDxe/TimerDxe.c` | 拿不到 `HardwareInterrupt` 协议时返回错误而非 `ASSERT` | GIC 未就绪时不停机 |
| 2 | `Mu_Basecore/MdeModulePkg/Library/MemoryProtectionHobLib/DxeMemoryProtectionHobLib.c` | 构造函数开头 `ZeroMem` + `return` | 内存保护设置不再出现 `0xFFFFFFFF` |
| 3 | `Mu_Tiano/ArmPkg/Drivers/CpuDxe/AArch64/Mmu.c` | `PageAttributeToGcdAttribute()` 的 `default` 分支去掉 `ASSERT(0)` | 未知页属性（0xFFFFFFFF）只打印不停机 |

**注意**：`page attribute 0xFFFFFFFF` 仍会打印（页表遍历读到无效项），暂不影响启动。

## 四·六、阶段 2 里程碑：RAM 盘载荷通路打通（2026-10-23 16:05）

实测屏幕输出：

```
PcdDxe.efi
R11RamDisk.efi
R11RamDisk: payload 10000 字节, 80 块, 发布 BlockIo     ← 0x10000 = 65536 B, 0x80 = 128 块
...
DiskIoDxe.efi
PartitionDxe.efi
EnglishDxe.efi
```

`DiskIoDxe / PartitionDxe / EnglishDxe` **首次出现在日志里**，证明
"固件卷 RAW 段 → BlockIo → 分区层 → 文件系统层" 这条链已经跑通。

### 实现组成

| 组件 | 文件 | 说明 |
|---|---|---|
| FAT12 生成器 | `scripts/make-payload-fat.py` | 零依赖，生成 64 KiB FAT12（`EFI/BOOT` 目录 + `README.TXT`） |
| 载荷驱动 | `r11Pkg/Drivers/R11RamDisk/` | 按 GUID 从 FV 取 RAW 段，发布只读 BlockIo（512B/块） |
| FDF 段 | `r11Pkg/Include/PAYLOAD.inc` | `FILE RAW = 6f2e1d3c-…-117d55318e01`，引用 `$(WORKSPACE)/Platforms/OPPO/r11Pkg/Resources/Payload/payload.fat` |
| `[Components]` | `r11.dsc` 末尾 | 追加语义（不会覆盖原列表） |
| 调度 | `DXE.inc` 第 2 行（进 FV）+ `APRIORI.inc` 第 6 行（早调度） | **两处都要写**；只写 APRIORI 不会打进 FV |

### 下一步：让 BDS 出现启动项

BDS 只把 `\EFI\BOOT\BOOTAA64.EFI` 当启动项，所以要在 FAT 里放一个真正的 UEFI 应用：

1. 在平台里加一个 `UEFI_APPLICATION`（例如打印 "R11 payload OK" 后返回）
2. **两遍构建**：先用第一遍产出的 `.efi` 生成 `payload.fat`，再重新打包 FD
   （构建脚本里加一个 `--payload-app` 模式即可）
3. 或者把 Mu 的 `Shell.efi` 作为应用塞进去，这样菜单里会出现 "Internal Shell"

## 四·五、阶段 2 前期现状（2026-10-23 15:55）

已经确认的调度顺序（日志实测）：

```
PcdDxe → EnvDxe → ReportStatusCodeRouter → StatusCodeHandler → RuntimeDxe
→ CryptDxe → WatchdogTimer → SecurityStubDxe → SmemDxe → DynamicRamDxe
(RAM Partition: 0x94000000-0x5ED00000, 0x01000000-0x7E400000)
→ HWIOBlockDxe → ChipInfo → PlatformInfoDxe → QdssDxe → ClockDxe
→ ULogDxe → NpaDxe
→ DALLOG [0x2000145]: Read boot log failed: efi no media
→ Disqualified - No Bootloader failed <No Media>
→ Error: Failed to GetBootLoaderInfo
```

**问题**：`DXE.inc` 里 `HWIODxe(55) / I2CDxe(56) / SPIDxe(57) / SPMIDxe(58) /
SdccDxe(59) / UFSDxe(60)` **一条都没被调度**——调度在 `ClockDxe` 之后就停住了。
因此 eMMC 从未初始化，厂商 `BootLoaderd` 读不到存储，报 `no media`。

### 结论：B 方案（RAM 盘）不可行

`NativeRamBlock` 的 README 写明：

> Linked into XNU by `unixnu build kernel`.

它是**链接进 XNU 内核**的驱动，不是 UEFI 侧。UEFI 里没有把它注册成 BlockIo 的驱动，
所以"用 ramdisk 让 BDS 出现启动项"在固件侧走不通。

### 阶段 2 的候选修法（按优先级）

1. **查清 ClockDxe 之后为什么停**：可能是某个驱动在等 NPA/时钟资源超时，
   或者 DEPEX 不满足而 DxeCore 卡在 Dispatcher 循环里。
   手段：把 `PcdDebugPrintErrorLevel` 改回全开，并在每个驱动前后加自定义打印（需要改 Mu 或写个探针 DXE）。
2. **把 SdccDxe 提到 ClockDxe 之后立刻调度**：顺序无关的可能性也存在（DxeCore 按 FV 顺序），
   但先排除"顺序导致等待"。
3. **用自己的 `NativeSdhci`**（`drivers/NativeSdhci/`，已有 PIO 单块读骨架）替换厂商 `SdccDxe`：
   eMMC 参数已知（`sdhci@c0c4000` 核心 + `0xc0c5000` vendor，IRQ 110/112，HS400 ES）。
4. **绕过存储**：把 payload 直接编进 FD（作为 FFS 内的一个 FAT 卷），由 `FatDxe` 从
   `RamDiskDxe` 的静态注册读——需要自己写一个 10 行的注册驱动。

## 五、下一步（阶段 2）

目标：让 BDS 能找到并读取启动载荷（UniXNU payload）。

### 5.1 让固件看到存储设备

现有 `Binaries/r11` 里已抽出厂商 `SdccDxe`（SDHCI 控制器驱动），需要：

1. 在 `Include/DXE.inc` 启用：
   - `Binaries/r11/QcomPkg/Drivers/SdccDxe/SdccDxe.inf`
   - `MdeModulePkg/Universal/Disk/DiskIoDxe/DiskIoDxe.inf`
   - `MdeModulePkg/Universal/Disk/PartitionDxe/PartitionDxe.inf`
   - `MdeModulePkg/Universal/Disk/UnicodeCollation/EnglishDxe/EnglishDxe.inf`
   - `FatPkg/EnhancedFatDxe/Fat.inf`
2. 若厂商 `SdccDxe` 依赖 `DALSYS`/`HWIO` 的配置表（`r11.conf` 里可能缺 sdc 条目），需要补 `PcdSdc*` 或 HWIO 配置。

### 5.2 或者先走"RAM 盘"路线

`unixnu bootimg` 不带 `--ramdisk none` 时会把 payload FAT 卷塞进 ramdisk。固件侧还需要：

- `MdeModulePkg/Universal/Disk/RamDiskDxe/RamDiskDxe.inf`
- 以及把 ramdisk 的物理地址/长度告知固件（BootShim 或 FDT）

这条路线不依赖 eMMC，能更快看到"菜单里有 xnu 项"。

### 5.3 已验证可用的调试手段

- **解包镜像**：`_installers/extract_fd_from_bootimg.py`（gzip → FD → FV 树 + 模块名）
- **解包 FV**：`unixnu-r11/scripts/extract-fv.py`（支持 LZMA 压缩卷）
- **分区校验**：TWRP `dd ... | md5sum`

## 六、设备/分区速查

| 项目 | 值 |
|---|---|
| boot | `mmcblk0p21`（64 MiB） |
| bootbak | `mmcblk0p22` |
| 回滚镜像 | `R11-backup/firmware/boot.img`、`bootbak.img` |
| TWRP | 已安装，可用 adb + dd |
| 面板 | 1080×1920 Samsung S6E3FA3，splash 区 `0x9D400000` + 0x2300000 |
| GIC | GICv3，`0x17A00000` / redistributor `0x17B00000` |

## 四·七、阶段 2 的硬阻塞：BDS 从未启动（2026-10-23 16:15）

实测日志（R11RamDisk 正常发布 BlockIo 之后）：

`
MetronomeDxe.efi → GLinkDxe.efi → ULogDxe.efi → NpaDxe.efi → ClockDxe.efi
DALLOG Device [0x2000145]:
Read boot log failed: efi no media,
Disqualified: Failed to ReadHeader Failed <No Media>
Error: Failed to GetBootLoaderHeaderInfo
`

- R11RamDisk 的 BlockIo 已经发布（DiskIoDxe / PartitionDxe / EnglishDxe 曾经出现过）
- 但**厂商的 BootLoaderd/DALLOG 在 ClockDxe 之后失败并停住**
- 因此 **BDS 从未开始执行**，不会去扫描 FAT 卷，\EFI\BOOT\BOOTAA64.EFI 也就不会被加载

### 结论

RAM 盘链路本身已经可用，但**它的价值取决于 BDS 能跑起来**。下一个目标是让 BDS 启动，
而不是继续完善 payload。

### 候选手段（下一步）

1. **裁剪厂商 DXE**：把 QdssDxe / ULogDxe / NpaDxe / ClockDxe / PlatformInfoDxe / GLinkDxe
   等从 DXE.inc 里去掉，看 BDS 是否出现（二分法，一次去掉一半）
2. **保留 Clock/Npa 但去掉 BootLoaderd 相关**：定位到底哪个模块打印
   Failed to GetBootLoaderHeaderInfo（它很可能在 TzDxe 或 PIL 里）
3. **让 SdccDxe 工作**：它依赖 Clock/Npa/DAL，与 1 冲突，优先级最低
4. 一旦 BDS 能跑，R11RamDisk + FAT + BOOTAA64.EFI 立刻就能验证（代码已就绪）

## 四·八、BDS 首次启动（2026-10-23 16:40）

去掉 NpaDxe / SdccDxe / UFSDxe 之后，DXE 调度完整走完，**BdsDxe 开始执行**：

`
Fat.efi → EnglishDxe → VariableRuntimeDxe → EmbeddedMonotonicCounter
→ ResetSystemRuntimeDxe → RealTimeClock → PrintDxe → DevicePathDxe
→ CapsuleRuntimeDxe → HiiDatabase → FontDxe
→ BdsDxe.efi                      ← 启动管理器
→ DALTLMM → I2C → SPI → PmicDxe
PmicDxe: DAL device (0x2000139) not found / Locate NpaProtocol failed
Read boot log failed: efi no media
Error: Failed to GetBootLoaderHeaderInfo
`

### 阶段 2 剩余的唯一一步

BDS 没有自动启动我们的卷，原因：

1. 没有 Boot#### 变量 / BootOrder 指向 R11RamDisk 发布的那个 FAT 卷
2. \EFI\BOOT\BOOTAA64.EFI 已在卷里（R11PayloadTest.efi，FAT 生成器已写入）
3. 没有输入设备，BDS 无法靠按键选择

**修法**：在 R11RamDisk 里主动构造启动项：

1. 给我们的块设备安装一个 MEDIA_VENDOR_DP 设备路径
2. ConnectController 让 FatDxe 挂载
3. 找到带 SimpleFileSystem 的子 handle，追加 FilePath(\EFI\BOOT\BOOTAA64.EFI)
4. SetVariable (LBoot0001, EFI_LOAD_OPTION) + BootOrder = L0001
5. BDS 就会自动加载并执行该 EFI 应用

### 当前 DXE 裁剪状态

| 已注释的厂商驱动 | 原因 |
|---|---|
| ULogDxe / QdssDxe / GLinkDxe / PlatformInfoDxe | 日志/跟踪，与启动无关 |
| NpaDxe | 断言 NULL 格式串；且其时钟服务不可用 |
| SdccDxe / UFSDxe | 依赖 NPA，取时钟失败（Protocol Error） |

备份：Include/DXE.inc.bak、Include/APRIORI.inc.bak（可整体回滚）

## 四·九、阶段 2 收尾状态（2026-10-23 17:00）

### 已确认可用的链路

```
固件卷 RAW section (FAT12, 含 EFI/BOOT/BOOTAA64.EFI)
  → R11RamDisk 驱动读取并发布 BlockIo          ✅ 日志证实
  → DevicePath + ReadyToBoot 钩子               ✅ 日志证实
  → DiskIoDxe / PartitionDxe / FatDxe 均加载    ✅
  → BdsDxe 启动                                 ✅
```

### 卡点：厂商二进制的 ASSERT 会 CpuDeadLoop

`NpaDxe` / `PmicDxe` 等预编译驱动里链接的是它们自己的 DebugLib，
`ASSERT` 用的 `PcdDebugPropertyMask` 是编译期常量（改 DSC 对它们无效），
断言后执行 `CpuDeadLoop()`：

- 摘掉 `NpaDxe` → `PmicDxe` 因 "Locate NpaProtocol failed" 断言 → 停
- 恢复 `NpaDxe` → `NpaDxe` 自己因 NULL 格式串断言 → 停

### 下一步

**全部摘掉需要 NPA 的厂商驱动**：
`NpaDxe / PmicDxe / ChargerExDxe / QcomChargerDxe / AdcDxe / TsensDxe /
LimitsDxe / MpPowerDxe / ClockDxe / DALSYSDxe`，只留 `SmemDxe`
（Mu 的 `DynamicRamDxe` 需要它）。

注意：之前"全摘"时出现过 data abort（FAR=0x8000000000000000），
需要单独定位是哪个 Mu 驱动在缺依赖时解引用了空指针。

### 本轮实现的文件

| 文件 | 作用 |
|---|---|
| `scripts/make-payload-fat.py` | 生成 FAT12（支持目录树、`--add` 注入文件） |
| `r11Pkg/Drivers/R11RamDisk/` | FV RAW → BlockIo → 注册 Boot0001 |
| `r11Pkg/Applications/R11PayloadTest/` | 验证用 UEFI 应用（打印 hello） |
| `r11Pkg/Include/PAYLOAD.inc` | 必须用 `FREEFORM + SECTION RAW`（不能用 `FILE RAW`） |
| `scripts/rebuild-clean.sh` | 全量重编 + FAT 生成 + 两遍构建 |
| `scripts/patch-mu.sh` | 4 个 Mu 补丁（幂等） |

### 回滚

- 驱动列表：`Include/DXE.inc.bak`、`Include/APRIORI.inc.bak`
- 手机：TWRP 里 `dd` 回 `R11-backup/firmware/boot.img`

## 四·十、阶段 2 达成：固件能加载并执行自定义 EFI 程序（2026-10-23 17:30）

### 证据链

```
PcdDxe.efi
R11RamDisk.efi
R11RamDisk: payload=10000 bytes blocks=80 BlockIo published
R11RamDisk: ReadyToBoot hook installed
R11RamDisk: payload app scheduled in 3s
...
（中间是 BDS/UI 的加载日志）
R11RamDisk: launching payload app (8000 bytes)      ← 定时器触发，LoadImage+StartImage
R11PayloadTest.efi                                  ← DxeCore 执行入口
（无 Synchronous Exception）
```

### 这条链的组成

| 步骤 | 实现 |
|---|---|
| FAT12 载荷卷 | `scripts/make-payload-fat.py` 生成，作为 `FREEFORM + SECTION RAW` 编进 FV |
| 取出 RAW 段 | `Fv->ReadSection (EFI_SECTION_RAW)` |
| 发布块设备 | 只读 `EFI_BLOCK_IO_PROTOCOL`（512B/块） |
| 设备路径 | `HW_VENDOR` 节点，让 FatDxe 可挂载 |
| 启动项 | `ReadyToBoot` 钩子里写 `Boot0001` + `BootOrder` |
| 直接执行载荷 | 3 秒定时器里按 GUID 取 `R11PayloadTest` 的 PE32 → `LoadImage`/`StartImage` |

### 学到的两个关键细节

1. **`FILE RAW` vs `SECTION RAW`**
   FDF 里 `FILE RAW = guid { file }` 生成的 FFS 文件没有 section 头，
   `ReadSection(EFI_SECTION_RAW)` 找不到；必须写成
   `FILE FREEFORM = guid { SECTION RAW = file }`。

2. **驱动运行时机与控制台**
   `R11RamDisk` 在 APRIORI 第 3 位，此时 `gST->ConOut` 仍是 NULL。
   应用若在那时打印，会在 `0x8`（`OutputString` 偏移）触发 Translation fault。
   所以载荷启动改由 **3 秒定时器**触发，等 `ConSplitter`/`GraphicsConsole` 就绪。

3. **应用必须列进 FDF**
   只在 `[Components]` 里写会编译但不进固件卷；要同时加进 `DXE.inc`。

### 仍然存在的问题（不阻塞阶段 2 结论）

- Mu 的图形 UI（`UiApp`/`RenderingEngineDxe`/OSK）在无厂商显示驱动的配置下会刷屏报错，
  且找不到 GOP 设备路径，最终自己崩在渲染引擎里。
- 我们的应用输出可能被 UI 切换的控制台吞掉；后续可把载荷启动移到
  `ReadyToBoot` 之后、或在应用里直接用 `gST->ConOut` 重新拿一次句柄。

### 回滚

- 驱动列表备份：`Include/DXE.inc.bak`、`Include/APRIORI.inc.bak`
- 手机：TWRP 里 `dd` 回 `R11-backup/firmware/boot.img`

## 四·十一、阶段 2 最终确认（2026-10-23 17:35）

**屏幕顶部出现白色横带 = 载荷应用在真实硬件上执行成功。**

这条白带由 `R11PayloadTest.efi` 直接写入 framebuffer（`0x9D400000`，顶部 150 行 ×
1088 像素，`0x00FFFFFF`），不经过 UEFI 控制台/BDS/文件系统。它出现即证明：

```
固件卷 → FAT12 载荷卷（FREEFORM + SECTION RAW）
      → R11RamDisk 按 GUID 取出 RAW 段
      → 发布只读 BlockIo + HW_VENDOR 设备路径
      → 3 秒定时器（等控制台就绪）
      → 按 GUID 取出 R11PayloadTest 的 PE32
      → gBS->LoadImage + gBS->StartImage
      → 应用在 AArch64 上执行 → 写 framebuffer → 白带
```

### 验证用行为说明

`R11PayloadTest.c` 当前是**验证模式**：

1. 尝试用 `ConOut->OutputString` 打印（BDS 之后可能已失效）
2. 直接写 framebuffer 顶部白带
3. `CpuDeadLoop()` 停住

**将来接入真实载荷时**，把 2、3 换回 `return EFI_SUCCESS`，
并把 `StartPayloadApp` 的目标 GUID 换成 UniXNU 的引导程序即可。

### 阶段 1 + 阶段 2 完成清单

| 能力 | 状态 |
|---|---|
| 自制 UEFI 固件在 R11 上加载运行 | ✅ |
| DXE 完整调度到 BDS | ✅ |
| 显示（SimpleFbDxe + cont-splash） | ✅ |
| 从固件卷发布 FAT12 载荷卷为块设备 | ✅ |
| 注册 `Boot0001` 启动项 | ✅（ReadyToBoot 钩子） |
| 直接加载并执行自定义 EFI 程序 | ✅ **屏幕白带为证** |
| 可信的构建/刷写闭环（TWRP dd + md5 回读） | ✅ |

### 未完成（阶段 3+，需要 macOS）

- `unixnu build kernel` 需要 Xcode（编译 XNU Mach-O 内核）
- 真正的 UniXNU payload（kernel + trust cache + Data 卷）
- eMMC 驱动（当前厂商 `SdccDxe` 依赖 NPA 时钟链，整条厂商栈被摘除）

### 当前固件的驱动取舍（重要）

`Include/DXE.inc` 现在只保留：

- 全部 Mu / EDK2 模块
- `Binaries/r11/QcomPkg/Drivers/SmemDxe`（Mu 的 DynamicRamDxe 需要）
- 我们的 `R11RamDisk`
- 验证应用 `R11PayloadTest`

其余厂商驱动全部注释。备份：`DXE.inc.bak` / `APRIORI.inc.bak`。

## 五、阶段 3：编译 XNU 内核（2026-10-23 17:45 开始）

### 硬性条件

`unixnu build kernel` 会检查 `xcodebuild`（iphoneos SDK），**只能在 macOS 上跑**。
Windows / WSL 做不了，这一步没有绕过的办法。

### 已完成的准备

| 项目 | 说明 |
|---|---|
| `scripts/check-stage3.py` | 就绪检查：工具链、board.toml 字段、脚本依赖、已有产物 |
| `.github/workflows/build-r11-kernel.yml` | 在 macos-14 runner 上编译内核 + 打包 payload 的 CI |
| `scripts/seed-data-skip-setup.py` | 从 UniXNU 上游复制（Data 卷跳过开机设置） |
| `board.toml` `panic_screen` | 改为 `false`（上游没有 `patch-xnu-entry-probe.py`） |

### 唯一缺口：`scripts/make-r11-adt.py`

payload 步骤这样调用它：

```
python3 scripts/make-r11-adt.py --output .../kernel.macho.adt \
  --dram-size 0xFE4C0000 --owned-memory-size 0x60000000 \
  --chip-id 207 --cpu-regs 0,0x1,0x2,0x3,0x100,0x101,0x102,0x103 \
  --psci-method smc --unique-chip-id <HEX> \
  --packed-data-ramdisk --trust-cache --ipad-identity --persistent-data \
  --text-fixup NAME,OFF,SIG,REP ...
```

它要生成 **Apple Device Tree（ADT）**——XNU 靠它识别平台拓扑（`/device-tree`、`/chosen`、
`/cpus`、`/arm-io`、`/memory`、`/defaults` 等节点）。这是 UniXNU 移植里最"逆向"的一步：
把 SDM660 描述成 XNU 认得的 Apple SoC 形态。

### 两条可行路径

**A. 从 iPad7,1 的 IPSW 里取参考 ADT，再打补丁**

1. `unixnu ipsw` 已经会提取 `DeviceTree` 组件（`board.toml` 的 `ipsw.fetch` 里有）
2. 写 `make-r11-adt.py`：读参考 ADT → 替换 `dram-size` / `chip-id` /
   `cpu-regs` / `boot-args` 等属性 → 输出 `kernel.macho.adt`
3. 需要先下载 IPSW（约 5 GB）

**B. 从已有的 UniXNU 设备移植里拿现成实现**

如果能看到另一个完成度较高的 `make-<device>-adt.py`（同 SoC 更好），
按它的结构适配 R11 的参数会快得多。

### 下载 IPSW 的命令（在任意平台均可）

```bash
cd /root/src/UniXNU
./unixnu ipsw -D /root/src/unixnu-r11        # 显示
./unixnu ipsw -D /root/src/unixnu-r11 extract  # 提取内核/设备树/信任缓存
```

---

# 阶段 3 记录（内核 + payload + 启动管理器）

> 记录时间：2026-10-24 01:00
> 结论：**内核已编出、payload 已打进 boot.img、payload 卷已挂载、启动管理器已取出并 StartImage**，
> 唯一卡点：`StartImage(UniXNU.efi)` 不进入应用入口。

## 3.1 已确认的设备限制（都是真机实测）

| 通道 | 限制 | 证据 |
|---|---|---|
| `ramdisk` 段 | **≤ 32 MiB** | 32 MiB 能启动；36 MiB 卡在 OPPO logo |
| `second` 段 | 镜像被接受，但 **ABL 不把内容加载进内存** | 全内存扫描（0x80000000–0x90000000、0x10000–0x10000000）找不到 |
| `kernel` 段解压后 | 61.8 MB 卡在 logo（3 MB 正常） | 内嵌 payload 方案失败 |

结论：**payload（56 MiB）必须压缩**才能通过 ramdisk（32 MiB 上限）送进内存。

## 3.2 最终采用的方案（已验证可行）

```
payload.img (56 MiB, FAT32)
   │  LZMA-Alone 压缩（回填解压后大小到头部 byte5..12）
   ▼
payload.img.lzma (6.4 MB)          ← < 32 MiB，放 ramdisk
   │  ABL 加载到 0x83400000（实测地址，不是 0x81000000）
   ▼
R11BootRamdisk（我们的 DXE 驱动）
   │  1) 扫描内存找 LZMA 流（props=0x5D、字典=8 MiB、大小 1..64 MiB）
   │  2) LzmaUefiDecompressGetInfo + LzmaUefiDecompress 解压到分配到的缓冲区
   │  3) 校验 FAT 引导扇区并发布只读 BlockIo
   ▼
DiskIoDxe → PartitionDxe → FatDxe → SimpleFileSystem
   │  ⚠ 必须主动 ConnectController：见 3.3
   ▼
R11BootRamdisk 从 FV 取出 UniXNU.efi 并 LoadImage + StartImage
```

固件日志（真机）：

```
R11BOOT: scan hit LZMA at 0x83400000, dest 58720256 B
R11BOOT: scan inflated 58720256 B at 0x2F43000 (FAT ok)
R11BOOT: payload volume at 0x2F43000, 0x3800000 bytes
R11BOOT: own volume: BlockIo Success DiskIo Success SimpleFS Success
R11BOOT: 1 filesystem(s) present
R11BOOT: starting UniXNU.efi (118000 bytes)
```

## 3.3 关键的三个坑（都踩过并绕开）

1. **定时器不可用**：固件里没有 Timer Arch Protocol（ArmTimerDxe 早期因 ASSERT 被移除），
   `SetTimer` 事件永不到期。→ 改用协议通知。
2. **ReadyToBoot 到不了**：BDS 画开机 UI 时崩（GOP/OSK 相关），
   `BdsArchProtocol` 都没装上。→ 不能依赖 BDS 之后的事件。
3. **协议通知要选对**：`SimpleFileSystem`/`DiskIo` 通知都不可靠；
   最终用 **`LoadedImage` 通知**（每个驱动装入都会触发），
   回调里主动 `ConnectController(ourHandle)`，才能把
   `BlockIo → DiskIo → Fat → SimpleFileSystem` 链建起来。

## 3.4 UniXNU 启动管理器的编译适配（Mu 版本差异）

本版 Mu 的 `FdtLib` **只有头文件、没有实现**，且缺 9 个 UniXNU 用到的函数。
我们新增 `r11Pkg/Library/R11FdtCompat`（约 300 行）自己实现：

| 函数 | 实现方式 |
|---|---|
| `FdtTotalSize` / `FdtCheckHeader` / `Fdt16/32/64ToCpu` | 读 FDT 头、大小端转换 |
| `FdtGetProp` / `FdtGetProperty` / `FdtGetName` / `FdtGetString` | 解析 struct/strings 块 |
| `FdtFirstSubnode` / `FdtNextSubnode` / `FdtSubnodeOffsetNameLen` | 结构块遍历 |
| `FdtPathOffset` | 按路径逐级匹配 |
| `FdtOpenInto` / `FdtPack` / `FdtStrerror` / `FdtTotalSize` | 安全降级 |
| `FdtOverlayApply` / `FdtSetProp` / `FdtAddSubnode` / `FdtDelProp` / `FdtDelNode` | 返回 -1/0（不支持改写） |

另外构建时给 UniXNULib 的源文件注入 Mu 缺失函数的**原型声明**
（`FdtPathOffset`/`FdtDelProp`/`FdtDelNode`/`FdtGetProp` 等，Mu 头里没有）。

## 3.5 当前唯一卡点

`StartImage(UniXNU.efi)` 调用后：

- 不返回（我们的 `UniXNU returned %r` 从未打印）
- 应用入口的标记也不打印（在 `UniXNU.c` 第一行插了 ConOut 写 `UNIXNU: app entry`）

推断：**入口根本没有被执行**（不是卡在 UniXNU 内部）。

已排除：
- 固件里确实有该应用（extract-fv 显示 `APPLICATION 118832 B` / `PE32 118784 B`）
- 编译产物里有补丁（`strings UniXNU.efi | grep -c 'app entry'` = 1）
- 阶段 2 时同样的 `LoadImage + StartImage` 成功启动过测试应用（白墙为证）

待验证的假设：
1. `LoadImage` 传入的 `DeviceHandle` 不对（我们传的是驱动自身的 `Self->DeviceHandle`）
2. 应用被加载到与 58 MB 解压缓冲区重叠的地址
3. 该 PE 的入口/重定位在本固件的页表下无法执行

## 3.6 下一步方案（按优先级）

| 方案 | 做法 | 预期 |
|---|---|---|
| **A. 把 UniXNU.efi 放进 payload 卷** | 用 mtools `mcopy` 把 `UniXNU.efi` 写进 `payload.img` 的 `\EFI\BOOT\BOOTAA64.EFI`，再用 SimpleFileSystem 的 `LoadImage`（带完整设备路径）启动 | 绕开 FV 加载路径，最接近 sheng 的原始形态 |
| B. 换 LoadImage 参数 | `DeviceHandle = NULL`，`FilePath` 用 FV 的 file path | 小改动，1 轮验证 |
| C. 缩小解压缓冲区并放到 0x3F000000 | 排除地址重叠 | 1 轮验证 |

## 3.7 已产出的构件

| 产物 | 位置 | 大小 |
|---|---|---|
| `kernel.macho` | CI artifact → `out/r11/kernel/` | 51.5 MB |
| `kernel.macho.adt` | payload 内，由 `make-r11-adt.py` 生成 | 13,704 B |
| `payload.img` | `out/r11/payload/` | 56 MiB |
| `boot.img` | `out/r11/boot/` | 7.4 MB |
| 固件 FD | `Build/r11Pkg/DEBUG_CLANGDWARF/FV/R11_UEFI.fd` | 3 MiB |

## 3.8 固件里现在有哪些模块

```
R11BootRamdisk   我们的 LZMA 解压 + BlockIo 发布 + UniXNU 启动器
UniXNUDxe        UniXNU 的 Boot#### 注册器（变量存储不可写，注册失败）
UniXNU           UniXNU 启动管理器（应用）
DarwinBoot       XNU 加载器（提供 UNIXNU_LOADER_PROTOCOL）
```

其余是 Mu/EDK2 的标准模块 + `SmemDxe`。

## 3.9 里程碑：DarwinBoot 完成内核交接（2026-10-24 01:43）

真机完整日志（OPPO R11）：

```
R11BOOT: scan hit LZMA at 0x83400000, dest 58720256 B
R11BOOT: scan inflated 58720256 B at 0x2F83000 (FAT ok)
R11BOOT: own volume: BlockIo Success DiskIo Success SimpleFS Success
R11BOOT: volume 0 open BOOTAA64.EFI -> Success
R11BOOT: image base 0xA2EE6000 size 0x1D000
R11BOOT: StartImage(from volume)

UNIXNU: UniXNU 0.1.0 starting as firmware boot option
UNIXNU: config: loaded \EFI\UniXNU\unixnu.toml
UNIXNU: config: 3 entries, 0 drivers, 0 dt patches, 0 acpi tables, 0 slices, 0 kits
UNIXNU: ui: headless (no GOP, no text console)
UNIXNU: menu: shown 3 entries, default 0 'xnu', timeout 3000 ms
UNIXNU: boot: entry 'xnu' (Boot)
UNIXNU: xnu: handing off 0 staged kext(s) to loader

DARWINBOOT: hand-off to the kernel
kernel  va 0xFFFFFFF00427C000  pa 0xA947C000
entry   pa 0xAC5E9488
args    pa 0xAD778000  window 0x60000000 bytes
CurrentEL 1  SCTLR_EL1 0x0C01B0D  TCR_EL1 0x280803518
ID_AA64MMFR0_EL1 0x1122  (PARange 2, 4 KiB granule supported)

Exiting boot services. A green bar across the top means the kernel entry code ran.
```

**这条链路已经完全打通**：

```
ABL → BootShim → UEFI DXE
  → R11BootRamdisk（LZMA 解压 payload → 发布 BlockIo）
  → DiskIoDxe → PartitionDxe → FatDxe → SimpleFileSystem
  → R11BootRamdisk 从卷里 LoadImage(\EFI\BOOT\BOOTAA64.EFI)
  → UniXNU（读 unixnu.toml → 菜单 → 选 'xnu'）
  → DarwinBoot（UNIXNU_LOADER_PROTOCOL：加载 kernel.macho + adt + boot_args）
  → 退出 boot services，跳转内核入口 0xAC5E9488
```

## 3.10 当前卡点：内核入口无输出

跳转后屏幕**保持文字不动**，也没有 DarwinBoot 提示的"顶部绿条"，
说明内核入口代码没有真正执行（或执行后立即挂死，且没有任何可见输出）。

可能原因（按可能性排序）：

1. **没有输出通道**：我们为通过编译关掉了 `BOOT_PROGRESS`、`NATIVE_LEGACY_DISPLAY`、
   `NATIVE_MOBILE_DISPLAY`，XNU 的 video console 没有任何驱动可用，
   即使内核在跑也看不到任何东西。
2. **内核早期就异常**：我们关闭了大量特性并加了桩（UFS store、legacy display probe），
   早期初始化可能直接失败。
3. **交接参数问题**：`window 0x60000000`（1.5 GB）可能与我们 58 MB 的 payload
   解压缓冲区（0x2F83000 起）或其他分配重叠。

## 3.11 下一步（按优先级）

| 方案 | 做法 | 目的 |
|---|---|---|
| **A. 给 XNU 一条输出通道** | 恢复 `CONFIG_XNU_NATIVE_LEGACY_DISPLAY`（需要参考内核候选）或写一个极简 framebuffer 打印 | 看到内核到底跑到哪 |
| B. 检查交接内存布局 | DarwinBoot 打印窗口的物理范围，和 payload/缓冲区比对是否重叠 | 排除覆盖 |
| C. 用 QEMU 复现 | 同一套 kernel.macho 在 QEMU 上跑（有串口输出） | 区分"内核问题"还是"R11 硬件问题" |

**当前成果**：内核已编出（51.5 MB）、ADT 已生成、payload 已能送达内存、
启动管理器与 XNU 加载器完整运行并完成内核交接。这是本项目最硬的骨架。

## 3.12 QEMU 路线的诊断记录（2026-10-24 上午）

目标：在 QEMU（AAVMF + vvfat ESP）里跑我们的内核，用串口拿到内核日志。

已经做到的：

* `qemu-system-aarch64` + AAVMF + `qemu-run.py` 能启动我们的 `BOOTAA64.EFI`
* `make-r11-adt.py --dram-base 0x40000000` 生成 QEMU 版 ADT
* `scripts/make-qemu-esp.py` 拼出含 UniXNU + DarwinBoot.efi + kernel.macho + ADT 的 ESP

卡点：UniXNU 自身在 AAVMF 下崩溃（硬件上同一个二进制正常）：

```
UNIXNU: app entry
UNIXNU: entry reached   x25          (BDS 在重试)
UNIXNU: step: log init done
Synchronous Exception at 0x7C74387C
```

崩溃被夹在 `UniXNULogInit()` 之后、`UniXNUReadFile()` 内部第一条语句之前：

* 与 kernel.macho 大小无关（1 MB 假内核同样崩）
* 与 toml 内容无关（官方 smoke ESP 同样崩）
* 与 GOP/UI 无关（`--no-gop` 同样崩）

最可能的两个原因：

1. `PcdGetPtr (PcdUniXNUConfigPath)` 在我们这套构建里拿到无效指针
2. vvfat 卷上的 `OpenPath` 走不通（硬件是真实 FAT32）

下一步：把探针放到 `UniXNUConfigLoad` 的 `PcdGetPtr` 之前（我们目前的探针都在 `UniXNUReadFile` 内部，而崩溃发生在更早）。

## 3.13 固件侧待办（从今晚的改动倒推）

* `USE_CUSTOM_DISPLAY_DRIVER = 0`（改用 SimpleFbDxe 提供 GOP）会引入
  "payload 在内存里找不到"的回归 —— 需要单独排查，但它是"内核有画面"的前提
* 解压缓冲区改为**入口预分配 64 MB**（`mDestBuffer`），避免后期碎片
* LZMA 扫描范围放宽到 `0x80000000..0xC0000000`（ramdisk 落点会随镜像大小变化）

## 3.14 内核挂在 start_cpu 的同步自旋（2026-10-24 11:00）

用 QEMU + gdb 单步得到的确切位置：

```
内核入口          0x4B1E9488   (pa) = 0xFFFFFFF0073E9488 (va)
断点命中          ✅  (gdb: Breakpoint 1, 0x000000004b1e9488)
死循环           0x4B1EB0EC   (pa) = 0xFFFFFFF0073EB0EC (va)
```

符号对照（`llvm-nm --defined-only -n kernel.macho`，va 为 16 位十六进制）：

```
fffffff0073eb00c  S _start_cpu
fffffff0073eb018  s start_cpu
fffffff0073eb05c  S _start_first_cpu      <-- 死循环在它 +0x90
```

即：**主 CPU 已经进入 XNU 的 ARM64 启动汇编，在 `_start_first_cpu` 里做 CPU 同步等待时自旋**，
之后再也到不了 `arm_init`。

现场指令（gdb `x/8i $pc`）：

```
0x4b1eb0ec: b.ne    0x4b1eb0e4      ; 外层循环
0x4b1eb0f0: adrp    x0, 0x4ae04000  ; x0 -> 0xFFFFFFF007004000 (mach header)
0x4b1eb0f8: ldr     w1, [x0, #24]   ; mach_header.flags
0x4b1eb0fc: tbz     w1, #31, ...    ; bit31=0 就跳过
0x4b1eb104: cmp     w1, #0x19       ; 0x19 = LC_SEGMENT_64
0x4b1eb108: b.ne    0x4b1eb108      ; 不等就死等
```

补充排查结论：

* mach header 本身正常（`flags = NOUNDEFS PIE`，bit31=0；首条 load command 就是
  `LC_SEGMENT_64 __TEXT 0xfffffff007004000`）→ 那段 `LC_SEGMENT_64` 自检会跳过，
  真正卡住的是它上面的 `b.ne 0x4b1eb0e4` 循环
* `-smp 8` 与 `-smp 2` 行为一致 → **不是 CPU 数量与 ADT 不符**
* QEMU 侧 `video base=0xBC7A0000 display=1 800x600x32`（手机侧是 `video base=0`）
* 手机侧连绿条都没有 → 手机死在跳转前，与内核代码无关

### 下一步要做的两件事

1. 读 XNU `osfmk/arm64/start.s` 的 `_start_first_cpu`/`start_cpu`，确认
   `0xFFFFFFF0073EB0E4..0EC` 这段循环在等哪个变量（大概率是 `CpuData`
   或 `machine_info` 的字段），再对照我们 ADT 对应字段
2. 用 QEMU 的 `gdb` 读取该循环等待的那个地址的值，看它为什么一直不满足

### 3.14 补充更正（读源码后）

对照 `out/feeds/xnu-0b4b910d/osfmk/arm64/start.s` 的 `start_first_cpu`：

* `0xFFFFFFF0073EB0E4..0EC` 那段 `b.ne 0x...B0E4` 是 **`Linvalidate_bootstrap`
  清页表循环**（`do { str x0,[x1],#8; entries-- } while (entries != 0)`，共 2048 次），
  **属于正常启动流程**，不是死循环——gdb 单步时恰好停在了它内部
* 紧随其后的才是 mach header 自检：

```asm
ldr w1, [x0, #0x18]                  // mach_header->flags
tbz w1, #0x1f, Lkernelcache_base_found   // MH_DYLIB_IN_CACHE(0x80000000)
ldr w1, [x0, #0x20]                  // 第一个 segment 的 cmd
cmp w1, #0x19                        // 必须是 LC_SEGMENT_64
bne .                                // 否则永不返回
```

而我们 `kernel.macho` 的 `flags = NOUNDEFS|PIE`（bit31 = 0），**这段会正常跳过**。

结论：**真正的挂起位置在 `_start_first_cpu` 之后**（`arm_init` = `0xFFFFFFF0079B1F0C` 附近），
下一轮应当直接在 `arm_init` 下断点，而不是在 `0x4B1EB108`。

## 3.15 内核跑到 IOKit + PAN 补丁（2026-10-24 下午）

### 3.15.1 根因：PAN 指令在 ARMv8.0 CPU 上不存在

XNU 按 ARMv8.1+ 编译，`arm_init` 里执行 `MSR PAN, #1`：

```
80:  d500419f    msr pan, #0x1      ; ARMv8.1 的 PAN 特性
```

骁龙 660 和 QEMU 的 cortex-a72 都是 **ARMv8.0**，没有 PAN →
Undefined Instruction（ESR EC=0）→ 异常向量未就绪（ELR/FAR=0）
→ 无限 Prefetch Abort → `panic("kernel panicked before debug and trace
infrastructure initialized")` → `halt_all_cpus` 自旋。

这解释了手机与 QEMU 两边"绿条之后全黑"的**同一个原因**。

### 3.15.2 补丁策略（scripts/patch-pan.py）

| 原指令 | 编码 | 替换为 |
|---|---|---|
| `MSR PAN, #imm` | `0xD500419F` | `NOP`（`0xD503201F`） |
| `MRS xN, PAN` | `0xD5384260\|N` | `MOV wN, #1`（`0x52800020\|N`） |
| 非法编码 | `0xE7FFDEFF` | `NOP` |

**注意**：`MRS` 不能换成 `MOV wN, #0`——XNU 在 `machine_switch_context`
里有断言：

```c
if (__builtin_arm_rsr("pan") == 0)
        panic("context switch with PAN disabled");
```

必须返回 **1**。

统计：MSR 15 条、MRS 14 条、非法编码 1 条，共 30 条。

### 3.15.3 打补丁后内核走到的位置

QEMU 执行轨迹（`-d in_asm` + 符号映射）显示内核已完整初始化并进入 IOKit：

```
_start → arm_init → 调度器(thread_invoke/machine_switch_context)
  → pmap/vm_map/IPC → IOKit: IOService::initialize
  → IOService::probeCandidates（驱动匹配）
  → IOCPU::start / IOPMrootDomain::start / IOAESAccelerator::createSpecialKeys
  → 最后仍在某处 panic（位于 __PLK_TEXT_EXEC，即预链接 kext 代码）
```

异常统计：`Undefined Instruction` 归零，只剩 IRQ。

### 3.15.4 新增工具脚本（都在 unixnu-r11/scripts/）

| 脚本 | 作用 |
|---|---|
| `patch-pan.py` | 打 PAN 补丁（幂等，带替换统计） |
| `qemu-run-r11.sh` | 一键：拼 ESP → 做 FAT32 镜像 → 跑 QEMU → 打印串口/debug_buf/截屏 |
| `read-debug-buf.py` | 通过 QMP 读内核 `_debug_buf`（panic 文字） |
| `qmp-shot.py` | QMP 截屏（PPM） |
| `make-qemu-esp.py` | 拼 QEMU 用 ESP |
| `dump-va.py` | 按 va 从 macho dump 字节给 objdump |
| `make-r11-adt.py --dram-base` | 生成 QEMU 版 ADT（DRAM 基址 0x40000000） |
| `_installers/analyze_inasm2.py`（Windows 侧） | in_asm 轨迹 → 内核符号映射 |

### 3.15.5 关键的 QEMU 使用要点

* **必须用真实 FAT32 镜像**（`mkfs.vfat` + `mcopy`），
  `-drive file=fat:dir` 的 vvfat 会让 UniXNU 崩（`_debug_buf` 未初始化）
* `-cpu cortex-a72`（v8.0）才能复现手机的 PAN 问题；`-cpu max` 反而掩盖它
* `-d in_asm -D log` 抓执行轨迹，是目前最有效的定位手段
* QMP `screendump` 看帧缓冲；`human-monitor-command` + `xp` 读内核内存

### 3.15.6 当前唯一的卡点

panic 发生在 `__PLK_TEXT_EXEC`（预链接 kext 代码），
调用链上最热的是 `IOCPU::start` / `IOService::probeCandidates`。
`IOCPU.cpp` 里的两处 panic（152/307 行）都不是 `start()` 里的，
需要继续用 in_asm 轨迹定位具体调用者（`_panic` 前的最后一个 PLK 块）。

## 3.16 排除法进展（2026-10-24 16:00）

用 `patch-func-ret0.py` 把 `IOAESAccelerator::start`（`0xFFFFFFF004A803EC`）
改成 `mov w0,#0; ret` 后重跑，**panic 位置与轨迹完全不变**（仅多 1 个块）：

```
run3: 内核块 32275，_panic 位置 32223
run4: 内核块 32276，_panic 位置 32224
```

→ 排除 `IOAESAccelerator`。

panic 前 300 块的热点函数：

```
_vm_object_destroy            30
IOCPU::start                  30
_vm_object_deallocate         17
_device_pager_last_unmap       9
```

即 panic 发生在 **VM 对象销毁 / device pager 解映射**路径上，
紧随 `IOCPU::start` 之后——典型的"某驱动 start 失败 → IOKit 回收内存 → 清理路径崩"。

`IOCPU::start` 源码（`iokit/Kernel/IOCPU.cpp:339`）本身没有 panic 点，
只是把 `gPEClockFrequencyInfo` 的三个频率写进 provider 属性后返回 true，
所以它不是元凶，但**紧随其后**。

### 下一步

1. 打印 `_panic` 前最后一个 PLK 块（`0xFFFFFFF0049BE570` 附近）
   的反汇编，看它传给 `panic()` 的字符串地址 → 读出 panic 原因
2. 或直接在 `device_pager_last_unmap` / `vm_object_destroy` 下断点看调用者
3. ADT 里 cpu 节点的 `timebase-frequency` 值得核对：QEMU 是 62.5 MHz
   （24 MHz 晶振 × 2.6），骁龙 660 是 19.2 MHz

## 3.17 地址映射澄清 + 为什么读不到 panic 文字（2026-10-24 16:20）

### 关键发现：运行期虚拟地址 == 符号表地址（slide = 0）

QEMU `info registers` 在 panic 自旋时报：

```
PC = 0xfffffff0072713cc
```

而符号表里 `_panic_trap_to_debugger = 0xFFFFFFF0072710D0`，
`0x72713cc - 0x72710d0 = 0x2FC` —— 完全吻合。

结论：**内核运行在 KVA 0xFFFFFFF007004000**（等于 macho 的 `__TEXT` vmaddr），
没有额外的 slide。物理地址换算：

```
pa(sym) = sym - (0xFFFFFFF007004000 - 0x4807C000)
        = sym - 0xFFFFFFEFBEF88000
```

（DarwinBoot 打印的 `va=0xFFFFFFF00427C000` 是另一个概念，不要拿它做 slide。）

### `_debug_buf` 确实是空的

用正确地址（`_debug_buf_base` 物理 0x48AF4668）读取，三个变量全为 0：

```
_debug_buf_base = 0x0
_debug_buf_ptr  = 0x0
_debug_buf_size = 0x0
_panic_info    = 全 0
```

说明 **`PE_init_kprintf` 没有运行**（或没分配缓冲区），
所以 `panic()` 的文字无处可写，内核只能"沉默自旋"。

### 现状总结

```
内核：初始化完成 → IOKit 匹配驱动 → IOCPU 注册 → panic
panic 位置：_panic_trap_to_debugger + 0x2FC（永久自旋）
panic 原因：读不到（kprintf 未初始化，debug_buf 为空）
```

### 下一步（优先级）

1. **让 kprintf 起来**：查 `PE_init_kprintf` 的调用条件
   （`pexpert/pexpert/arm/pe_init.c`），看它是否因为缺少
   `PE_state.video` / 串口 / `PE_parse_boot_argn` 的某个参数而提前返回
2. 一旦 kprintf 工作，panic 文字会进入 debug_buf，可直接读出
3. 备选：给 XNU 编译一个极简 `PE_init_kprintf` 替代（直接往
   `boot_args->Video.v_baseAddr` 的帧缓冲写字符）

## 3.18 尝试打开内核输出（2026-10-24 16:25）

`pexpert/arm/pe_kprintf.c` 里两个开关默认是关的：

```c
SECURITY_READ_ONLY_LATE(bool) disable_serial_output  = true;
SECURITY_READ_ONLY_LATE(bool) disable_kprintf_output = true;   // RELEASE 构建里永远 true
```

符号地址：

```
_disable_serial_output        0xFFFFFFF0071F9B50
_disable_kprintf_output       0xFFFFFFF0071F9B51
_disable_iolog_serial_output  0xFFFFFFF0071F9B52
```

用 `scripts/patch-kprintf-on.py` 把三个字节置 0 后重跑：

* 帧缓冲截图与之前**完全一样**（仍是 UniXNU/DarwinBoot 的文字 + 绿条）
* `_debug_buf` 仍为 0

说明 panic 输出走的不是这条路径，或 `PE_state.video` 没有被初始化
（`console_write_unbuffered` 依赖它）。

### 下一步要做的事

1. 看 `pexpert/arm/pe_video.c` / `pe_init.c` 里 `PE_state.video` 何时被填充
   （很可能在 `PE_init_platform` / `PE_create_console`，panic 时还没跑到）
2. 给 XNU 加一个"最简 panic 打印"：把 `panic()` 的第一行直接写进
   `boot_args->Video.v_baseAddr` 的帧缓冲（需要自己算字模/滚动，工作量中等）
3. 或者换思路：**不依赖输出**，用二分法/排除法逐个确认 IOKit 里哪个
   `start()` 之后会 panic（目前已确认不是 IOAESAccelerator）

## 3.19 panic 原文定位：AppleARMCPU 校验（2026-10-24 16:30）

用 gdb 直接读**虚拟地址**（QEMU gdbstub 会做 MMU 翻译），终于拿到 panic 现场：

```
pc = 0xFFFFFFF0072713CC      (_panic_trap_to_debugger + 0x2FC，wfe 自旋)
x1 = 0xFFFFFFF0070486D6  -> "panic"
x2 = 0xFFFFFFF004289E44  -> "\"CPU configuration in device tree is invalid\" @%s:%d"
邻近字符串           -> "AppleARMCPU.cpp"
```

所以 panic 来自 **kext `AppleARMCPU.cpp` 的 `_validateConfiguration()`**，它校验设备树 `/cpus`：

* `/cpus` 节点必须存在
* `max_cpus` 属性长度必须正确
* 最大 CPU 数不能为 0，实际 CPU 数不能超过 max
* 每个 CPU 需要 `cpu-id` / `interrupts` 等；若不用 AIC，必须有
  `no-aic-ipi-required`；定义了 `function-ipi_dispatch` 又会反过来报错

### 做过的尝试

1. `/cpus` 加 `no-aic-ipi-required = 1` → panic 位置未变
2. 把 `AppleARMCPU::_validateConfiguration()`（va `0xFFFFFFF004986AC4`）
   入口改成 `mov w0,#1; ret` → panic 前热点变成
   `IOCPU::start`(30) / `ArmCpu::~ArmCpu` / `IOCPU::~IOCPU`，
   即 CPU 对象被创建后又被销毁，仍在稍后 panic

### 下一步

* 查 `ArmCpu::start()` 需要什么（字符串池里有 `function-cpu_idle`、
  `function-platform_cache`、`function-enable_core`、`function-error_handler`、
  `Primary%ld` 等名字）——这些是 AppleARMCPU 期望的设备树属性
* 或者给 ADT 的每个 CPU 补 `cpu-id`（u32，0..N）与 `interrupts`
* 关键教训：**gdb 读虚拟地址**是唯一可靠的取字符串方式
  （内核会重定位，物理地址算不准）

### 工具沉淀

`scripts/gdb-read-va.sh`、`gdb-strings-va.sh`（gdb 读虚拟地址 + 字符串 +
反汇编）、`patch-func-retval.py`（函数改返回值）、`qmp-dump-reg-strings.py`。

## 3.20 里程碑：XNU 内核启动成功，进入 idle 循环（2026-10-24 16:45）

### 突破点

把 `AppleARMCPU::_validateConfiguration()` 的返回值判定改成恒真：

```
0xFFFFFFF0049871A0:  and w0, w19, #1   →   mov w0, #1
```

（该校验会做大量检查、失败时只记日志并把 w19 置 0，末尾统一 panic。
入口直接 return true 会跳过必要的数组分配，所以必须改**返回值**这一条。）

### 执行轨迹对比（QEMU -d in_asm）

```
run11（补丁前）: 32186 个内核块，panic 命中 1 次
run12（补丁后）: 43924 个内核块，panic 命中 0 次
```

最后 25 个执行块：

```
_machine_idle / _processor_idle / _idle_thread / Idle_load_context
_sched_clutch_processor_queue_empty / _sched_clutch_processor_csw_check
_compute_averages / _smr_cpu_tick
```

**内核已进入 idle 循环** —— 即 XNU 完成了全部早期初始化。

轨迹里还出现（说明走得很远）：

```
IOFindBSDRoot                 ← 正在查找根文件系统
IODTNVRAM::syncInternal       ← NVRAM 驱动工作
IOAESAccelerator::createSpecialKeys
thread_select / gate_steal / idle_thread
```

### 完整启动链（全部实证）

```
ABL → UEFI 固件 → LZMA payload(6.7MB) → FAT 卷
  → UniXNU 启动管理器 → DarwinBoot → kernel.macho
    → _start → arm_init（30 条 PAN 补丁）
      → 调度器 / pmap / vm_map / IPC
        → IOKit 驱动匹配 → AppleARMCPU 校验（返回值补丁）
          → IODTNVRAM / IOFindBSDRoot
            → idle 循环 ✅
```

### 剩余工作

1. **panic_screen**：`board.toml` 里 `panic_screen = false`，注释说上游缺
   `patch-xnu-entry-probe.py`；UniXNU 只有 `check-panic-entry-coverage.py`。
   需要补上这个补丁脚本，或自己实现"panic 输出到帧缓冲"
2. **Data seed / BSD root**：内核在 `IOFindBSDRoot` 找不到根。
   需要生成 Data 卷（sheng 用 `scripts/seed-data-skip-setup.py`），
   payload 从 56 MiB 扩大（压缩后仍 < 32 MiB 即可）
3. 之后才是驱动（显示、存储、输入）与用户态

## 3.21 里程碑：XNU 内核在真机 OPPO R11 上执行（2026-10-24 17:00）

### 真机验证

手机屏幕（boot-con.img）：

```
R11BOOT: prealloc decompress buffer at 0x2F87000 (64 MB)
R11BOOT: mapscan hit at 0x83400000 (guess 58720256 B)
R11BOOT: mapscan inflated 58720256 B at 0x2F87000 (FAT ok)
R11BOOT: payload volume at 0x2F87000, 0x3800000 bytes
R11BOOT: volume 0 open BOOTAA64.EFI -> Success
R11BOOT: StartImage(from volume)
UNIXNU: UniXNU 0.1.0 starting as firmware boot option
UNIXNU: config: loaded \EFI\UniXNU\unixnu.toml
UNIXNU: boot: entry 'xnu' (Boot)
UNIXNU: xnu: handing off 0 staged kext(s) to loader
DARWINBOOT: hand-off to the kernel
kernel va 0xFFFFFFF00427C000  pa 0xA947C000
entry  pa 0xAC5E9488
args   pa 0xAD778000  window 0x60000000 bytes
video  base 0x9D400000 1080x1920 row 4320 bytes 32 bpp
Exiting boot services. A green bar across the top means the kernel entry code ran.
```

**屏幕顶部出现绿色横条** → XNU 内核入口代码在真机上执行 ✅

### 这一天解决的全部问题

| # | 问题 | 解法 | 位置 |
|---|---|---|---|
| 1 | PAN 指令（ARMv8.1）在骁龙660（v8.0）上 Undefined | 30 处补丁：`msr pan,#1`→nop，`mrs xN,pan`→`mov wN,#1` | `scripts/patch-pan.py` |
| 2 | `machine_switch_context` 断言"PAN disabled" | 同上（必须返回 1 而不是 0） | — |
| 3 | panic 原因不可见 | **gdb 读虚拟地址**（内核会重定位，物理地址算不准）+ 字符串池定位 | `scripts/gdb-strings-va.sh` |
| 4 | `AppleARMCPU::_validateConfiguration` 返回 false → panic | 返回值判定 `and w0,w19,#1` → `mov w0,#1` | `0xFFFFFFF0049871A0` |
| 5 | 手机固件 `no payload volume` | 按 **UEFI 内存映射表**扫描（不再盲扫固定范围，避免 Data abort） | `R11BootRamdisk.c` |
| 6 | 扫描时 Reserved 区被跳过 | 把 `EfiReservedMemoryType` 加入扫描（ABL 把 ramdisk 放这里） | 同上 |
| 7 | 解压缓冲区在扫描时还是 NULL | 64 MB 预分配挪到驱动入口最前面 | 同上 |
| 8 | 手机无 GOP → `boot_args->Video` 全 0 | DarwinBoot 用 `0x9D400000/1080x1920/4320/32` 兜底 | `scripts/patch-darwinboot-video.py` |
| 9 | ARM64 上控制台默认走串口（不存在） | `cons_ops_index` 从 0 改成 1（视频控制台） | `0xFFFFFFF007167920` |

### QEMU 侧已证明内核完整启动

同一份内核在 QEMU 里执行轨迹：

```
run11（补丁前）: 32186 块, panic 1 次
run12（补丁后）: 43924 块, panic 0 次
最后 25 个块: _machine_idle / _processor_idle / _idle_thread
              _sched_clutch_processor_queue_empty / _compute_averages
中途: IOFindBSDRoot / IODTNVRAM::syncInternal / IOAESAccelerator
```

→ 内核完成全部早期初始化，**进入 idle 循环**。

### 仍然差的一步：内核日志上屏

`cons_ops_index` 已改为视频控制台，但屏幕仍无内核文字。
`vcputc` 依赖 `vc_initialized`（`vc_initialize()` 由 `PE_init_console` 调用），
很可能因为 `PE_state.video` 的某些字段/字体/映射条件不满足而没有初始化。

下一步方向：
1. 查 `osfmk/console/video_console.c` 的 `vc_initialize` 前置条件
2. 或直接在 DarwinBoot 里把 boot_args 的 Video 字段按 XNU 期望的**编码**填好
3. 或绕开：给内核加一个极简的"直接写帧缓冲"打印函数

## 3.22 内核"打印了但屏幕没字"的根因：gc_ops 装错（2026-10-24 17:20）

QEMU 轨迹证明内核确实在输出：

```
✅ _kprintf
✅ __doprnt_log
✅ _vcputc_options
✅ _initialize_screen
✅ _PE_init_kprintf / _PE_init_kprintf_config
```

且 `_vinfo`（0xFFFFFFF007AC6970）内容完全正确：

```
v_height   = 600
v_width    = 800
v_depth    = 32
v_rowbytes = 3200
v_baseaddr = 0xFFFFFFF08F800000   (映射后 VA)
v_physaddr = 0xBC7A0000           (ramfb 物理地址)
```

但 `_gc_ops`（0xFFFFFFF007B2F028）只有第一个指针非零，且它指向：

```
0xFFFFFFF0073BBBF8 = _vc_update_color     ← 槽位装错了
应为 0xFFFFFFF0073BA878 = _vc_paint_char
```

**结论：`gc_ops` 函数表没有被正确初始化**，`vcputc` 调用到一个只更新颜色的函数，
所以字符永远画不到帧缓冲上。这就是内核日志"看得见调用、看不见文字"的原因。

### 修法

* **A（快）**：二进制补丁把 `gc_ops.paint_char` 指向 `_vc_paint_char`，
  `clear_screen` 指向 `_vc_clear_screen`，其余槽位按需补齐
* **B**：查 `osfmk/console/video_console.c` / `gc.c` 里初始化 `gc_ops` 的代码，
  找出为什么只设了一个槽位

### 相关符号表（kernel.macho）

```
_vc_paint_char      0xFFFFFFF0073BA878
_vc_clear_screen    0xFFFFFFF0073BB938
_vc_update_color    0xFFFFFFF0073BBBF8
_vinfo              0xFFFFFFF007AC6970
_gc_ops (表)        0xFFFFFFF007B2F028
_cons_ops_index     0xFFFFFFF007167920
_PE_kputc           0xFFFFFFF0071F9B58
```

## 3.23 自写 GOP 驱动 + UniXNU 界面崩溃（2026-10-25 凌晨）

### 背景

`SimpleFbDxe` 在本机装不上 GOP（`LocateMemoryMapAreaByName` 失败），
厂商 `DisplayDxe` 的二进制（`Binaries/r11/QcomPkg/Drivers/`）是空的，
所以自己写了 `R11FbGop`：

```
平台参数（真机实测）
  基址    0x9D400000
  分辨率  1080 x 1920
  真实 pitch 1088 像素（不是 1080）
  格式    BGRx8888
```

实现：单模式 GOP + Blt（VideoFill / BufferToVideo / VideoToBuffer / VideoToVideo）。

### 踩的坑

1. 只加到 DSC **[Components]** 和 `APRIORI.inc` 不够——
   还必须加到 **`DXE.inc`**（那才是"放进 FV 的模块列表"），否则编了但不进固件
2. `EFI_DEVICE_PATH_PROTOCOL.Length` 是 `UINT8[2]`，不能直接赋值，
   要用 `SetDevicePathNodeLength` / `SetDevicePathEndNode`
3. `EFI_PIXEL_BITMASK` 没有 `Reserved0` 字段

### 结果

```
R11GOP: install Success, fb 0x9D400000 stride 1088 1080x1920     ← GOP 装上
R11GOP: present, mode 0, fb 0x9D400000 size 0x2400000 1080x1920   ← 后续能查到
```

但 UniXNU 开始画界面时崩溃：

```
Synchronous Exception at 0x00000000A2EA9650
X0 = FAR = 0xBE12FEF15B52FEE8
Data abort: Translation fault, zeroth level
```

崩溃地址在 UniXNU 镜像（0xA2E80000 + 0x1E000）之外，属于别的模块；
`ui: headless` 时这条路径从不执行，有了 GOP 才走进来。
把 `PixelsPerScanLine` 改成 1080（与 width 一致）并给 Blt 加防御后仍然崩。

### 决定

**暂时停用 R11FbGop**（DXE.inc / APRIORI.inc / DSC 里都注释掉），
回到已验证的稳定状态：headless UniXNU → DarwinBoot 帧缓冲兜底 → 内核入口（绿条）。

代码保留在 `r11Pkg/Drivers/R11FbGop/`，需要时重新启用继续调试。

### 下一步（恢复图形界面时）

1. 找出 `0xA2EA9650` 属于哪个 FV 模块（解包 FV 比对加载地址）
2. 或给 UniXNU 的绘制代码加更细的探针
3. `Blt` 目前每类操作最多打印 12 次调用参数，可以直接看出哪次参数异常

## 3.24 UniXNU 图形模式 + 绘制崩溃（2026-10-25 00:45）

### 进展：界面模式激活

重新启用 `R11FbGop` 后，手机上出现：

```
R11GOP: self 0x9F8E9000..0x9F8EB000
R11GOP: install Success, fb 0x9D400000 stride 1088 1080x1920
R11GOP: present, mode 0, fb 0x9D400000 size 0x2400000 1080x1920
...
UNIXNU: ui: graphics 1080x1920, scale 4, portrait layout      ← 图形模式！
```

**UniXNU 不再 headless，走进了图形界面代码路径。**

### 崩溃

```
Synchronous Exception at 0x00000000A808B778
X0 = FAR = 0xBE12FEF15B52FEE8
Data abort: Translation fault, zeroth level
```

* 崩溃代码地址 `0xA808B778` **不在** 我们驱动的范围（0x9F8E9000..0x9F8EB000），
  也不在 UniXNU 的范围（0xA2E80000..0xA2E9E000）
* 但栈里出现我们驱动的数据指针 `0x9F8EADF4` / `0x9F8EA5F4`，
  说明调用链**经过了 R11FbGop**
* `R11GOP: blt#N` 一条都没打印出来，说明崩溃发生在 Blt 的第一条语句之前
  （或者 DEBUG 输出在那一刻被中断）

### 已加的探针（下次继续用）

| 探针 | 输出 |
|---|---|
| `R11FbGop` 安装时 | `R11GOP: self <base>..<limit>` |
| 每次 Blt | `R11GOP: blt#N op=? dst(x,y) WxH`（最多 40 条） |
| 前两次驱动通知 | `R11IMG: <base>..<limit> (KB)` 全部已加载镜像 |

`R11IMG` 那批行在日志最顶部，需要开机后立刻截图（或改成分页打印）。

### 下一步

1. 抓到 `R11IMG` 全表 → 找出 `0xA808B000` 是哪个模块
   （可先在 `R11BootRamdisk` 里把 `DumpLoadedImages` 改成"每 5 个一屏 + Stall"，
   或者只打印 base 在 0xA8000000 以上的）
2. 或者干脆绕过 UniXNU 的绘制：给 `[ui]` 加 `console = "none"`，
   让它回到 headless（保留 GOP 给 DarwinBoot 用）

## 3.25 里程碑：UniXNU 图形界面在真机上显示（2026-10-25 01:15）

### 真机截图内容

```
🍎 UniXNU
version 0.1.0
config: \EFI\UniXNU\unixnu.toml

┌──────────────────────────┐
│ [1]  Boot      xnu       │  ← 蓝色高亮
├──────────────────────────┤
│ [2]  Restart   action    │
├──────────────────────────┤
│ [3]  Power off action    │
└──────────────────────────┘

        Booting Boot in 3 s
        Select the default entry
        boots when the timer ends

      0 loaded / 0 failed   kits 0
      acpi 0
```

**UniXNU 的完整启动菜单在 OPPO R11 上正常显示，无斜切、无错位。**

### 根因：帧缓冲 pitch 是 1080，不是 1088

之前 `R11_FB_STRIDE = 1088` 时，全屏填充（1080x1920）会写到：

```
0x9D400000 + 1088 * 1920 * 4 = 0x9D400000 + 8,355,840 B
```

而固件只映射了 `1080 * 1920 * 4 = 8,294,400 B`（正好一屏），
超出约 61 KB → **Translation fault（Data abort, zeroth level）**。

用 1088 时的现象：写到屏幕最后几行才崩，前 512 行完全正常
（这也是为什么 `R11_FB_FILL_MAX_ROWS = 512` 的测试不崩）。

改成 `R11_FB_STRIDE = 1080` 后：全屏填充 + 贴图全部正常。

### 定位过程（二分法）

| 步骤 | 开关 | 结果 | 结论 |
|---|---|---|---|
| 1 | `R11_FB_NOOP_BLT = 1` | 不崩，UniXNU 走完全流程 | 崩溃在绘制代码里 |
| 2 | 只画 `op=2`（贴图） | 不崩，屏幕出现 Logo 碎片 | 贴图路径 OK |
| 3 | 只画 `op=0`（填充） | **崩** | 填充是元凶 |
| 4 | 填充限制 512 行 | 不崩，屏幕出现大片色块 | 是"写越界"而非逻辑错误 |
| 5 | stride 改 1080，全屏填充 | **完美显示界面** | pitch = 1080 ✓ |

### 保留的调试开关（`R11FbGop.c` 顶部）

```c
#define R11_FB_STRIDE          1080        /* 关键：必须等于 width */
#define R11_FB_NOOP_BLT        0           /* 1 = 什么都不画 */
#define R11_FB_SKIP_FILL       0           /* 1 = 跳过 EfiBltVideoFill */
#define R11_FB_SKIP_BUFFER     0           /* 1 = 跳过 EfiBltBufferToVideo */
#define R11_FB_FILL_MAX_ROWS   0           /* >0 = 限制填充行数（探测边界用） */
```

### 附带结论

* `R11GOP: install Success` / `R11GOP: self <base>..<limit>` 探针保留（打印上限已降到 5 条）
* 安装时画的那条绿条已移除
* 固件自己的文字控制台仍会在同一帧缓冲上画日志，所以 UI 下方会残留一些调试文字
  （UniXNU 进入图形模式后不再往 ConOut 输出，残留来自之前的日志）

## 3.26 按键输入：SPMI/PMIC 探查（2026-10-25 01:40）

### 硬件事实（来自原厂设备树）

```
gpio_keys {
  vol_up   { gpios = <&pm660l-gpio 8 1>; linux,code = <115>; }   // GPIO 8
  vol_down { gpios = <&pm660l-gpio 5 1>; linux,code = <114>; }   // GPIO 5
}
pm660l-gpio { gpio@c000 { reg = <0xc000 0x100>; } }              // 外设基址 0xc000
```

* 三个键都不是 SoC 引脚，而是 **PMIC（PM660L）的输入**
* 必须经 **SPMI 总线 + PMIC arbiter** 才能读到
* R11 的 arbiter（设备树 `qcom,spmi@800f000`）：

```
core   0x0800F000  0x1000      版本 / channel 映射
chnls  0x08400000  0x1000000
obsrvr 0x09400000  0x1000000   观察者通道（读数据走这里）
intr   0x0A400000  0x220000
cnfg   0x0800A000  0x3000
qcom,ee = 0
```

### 已写驱动：`r11Pkg/Drivers/R11KeysDxe`

* SPMI 观察者通道读（命令字 `[27]=read`、`[11:4]=寄存器`、`[2:0]=字节数-1`）
* channel 表查找（core 窗口寄存器 `0x800 + 4*n`，`[19:8] = SID<<8|PID`）
* 音量键 → ScanCode Up/Down，提供 `SimpleTextInput` + `SimpleTextInputEx`
* 20 ms 定时轮询
* **必须放进 APRIORI**，否则我们自己的启动器在它调度前就跳进 UniXNU 了

### 真机结果

```
R11KEYS: arbiter version 0x30000000      ← arbiter v3，版本寄存器可读
（之后卡住，map[] 诊断行滚出屏幕）
```

### 下一步（明天从这里开始）

1. **读回 `map[0..7]`** —— 判断 channel 表在 `0x800+4n` 还是 `0x900+4n`，
   或者需要按 `chnls` 区域 0x8000 步进取表
2. `PM660L_GPIO_SID` 可能是 0 而不是 1（PM660 = 0，PM660L = 1，需确认）
3. 观察者通道偏移（`0x10000/EE`、`0x80/channel`、CMD 0x0 / STATUS 0x8 / DATA 0x18）
   在 v3 上是否相同 —— 参考 `hackios-xnu` 的 `redwood_buttons.c`（arbiter v5 的完整实现）
4. 音量键状态读 `GPIO STATUS1/STATUS2`（0x0A/0x0B，每字节 8 个引脚，低有效）

### 为什么这条链值得做

* UniXNU 现在显示 `No input device`，只能 3 秒超时自动启动
* 有按键后：音量键选菜单、电源键确认
* 这也是后续 XNU 侧输入驱动（`redwood_buttons.c` 式的 BSD 驱动）的预研

## 3.27 按键：SPMI 打通（2026-10-25 02:05）

### 已验证的真机输出

```
R11KEYS: self 0xA807A000..0xA8083000
R11GOP: install Success, fb 0x9D400000 stride 1080 1080x1920
R11KEYS: arbiter version 0x30000000              ← arbiter v3
R11KEYS: map[0..7] = 00000000 0000D000 00022000 00022100 00000000 00000900 00018900 00010000
R11KEYS: found ch 83 for pid 0x1D0 (map 001D000) ← 通道 0x53
R11KEYS: channel gpio=0x53 pon=0x53
R11KEYS: install Success
R11KEYS: poll timer Success
R11KEYS: poll#1 ver=0x30000000 ch=0x53/0x53
R11KEYS: VOL_UP                                  ← ★ 真的读到了按键
R11KEYS: VOL_DOWN                                ← ★
```

UniXNU 侧：

```
R11IN: ConIn=0x3EEA0 ConOut=0x3FEFB0
R11IN: 3 SimpleTextIn handle(s)
R11IN: physical=2 (UniXNU needs >0)
UNIXNU: menu: shown 3 entries, default 0 'xnu', timeout 15000 ms, input yes, rotate 0 ms
```
以及界面上的 `Up/Down: select    Enter: boot    1-9: boot entry    Esc: stop timer`。

### 关键参数（已确定）

| 项 | 值 |
|---|---|
| arbiter 版本 | v3（0x30000000） |
| core | 0x0800F000（版本寄存器 0x0 可读） |
| channel 映射表 | core 内 `0x800 + 4*n`，`[19:8] = SID<<8|PID` |
| PM660L GPIO 外设 | PID 0x1D0（SID 1，寄存器基址 0x8D00 → `>>8`） |
| 通道号 | 0x53 |
| 观察者通道 | 0x09400000 + 0x80*0x53 = 0x09402980 |
| 命令字 | `(1<<27) | (reg<<4) | (nbytes-1)` |
| STATUS/DATA | +0x08 / +0x18 |
| 音量上 | GPIO 8 → STATUS2(0x0B) bit0，低有效 |
| 音量下 | GPIO 5 → STATUS1(0x0A) bit5，低有效 |

### 踩过的坑

1. `EVT_NOTIFY_WAIT` 事件**必须带回调函数**，传 NULL 会返回 `EFI_INVALID_PARAMETER`，
   导致 `WaitForKey` 为空，ConSplitter 收不到按键通知
2. UniXNU 的 `CountInputDevices()` 要求：`gST->ConIn != NULL`
   **且**存在带 DevicePath 的 `SimpleTextIn` 句柄
3. 启动器（R11BootRamdisk）必须排在 `ConSplitterDxe` / `GraphicsConsoleDxe` **之后**，
   否则 UniXNU 启动时 ConIn 还不存在 → 永远 `input no`

### 剩余问题

进入 UniXNU 后**瞬间黑屏**（菜单一闪而过）——某个按键事件让菜单立即退出。
日志里 `VOL_UP`/`VOL_DOWN` 能正常打印，说明 SPMI 侧没问题。

### 明天的方案：直接覆盖 gST->ConIn

```
1. 驱动在 BDS 之后运行（或在 R11BootRamdisk 启动 UniXNU 前）
2. 保存 mOriginalConIn = gST->ConIn
3. gST->ConIn = 我们自己的 SIMPLE_TEXT_INPUT
   ReadKeyStroke：先读我们的按键队列；没有则转发 mOriginalConIn
   WaitForKey：我们的事件，按键时 SignalEvent
4. UniXNU 读 gST->ConIn 直接命中我们的实现 → 100% 收到按键
```

这样彻底绕开 ConSplitter 的聚合逻辑，不依赖它的绑定时机。

## 3.28 按键最后一层：ConSplitter 没接纳我们的设备（2026-10-25 02:40）

### 本轮学到的事实

| 观察 | 含义 |
|---|---|
| `R11KEYS: connect self Not Found` | 没有驱动愿意绑定我们的输入设备句柄 |
| `input yes` 但按键无效 | UniXNU 只数"物理句柄"，ConIn 里并没有我们 |
| 加 `gEfiConsoleInDeviceGuid` 后仍无效 | ConSplitter 启动时的扫描已经结束，后到的设备没被加入 |
| 加 GCD 检查后连 `VOL_*` 都不打印 | `gDS->GetMemorySpaceDescriptor(0x09402980)` 返回不存在 → 读取被拦掉（**教训：不要用 GCD 判断 SPMI 观察者区可达性**） |
| 定时器改成 `TPL_CALLBACK` 后没有任何轮询日志 | 本环境的 CALLBACK 通知不被派发，**必须用 `TPL_NOTIFY`** |

### 结论：剩下的就是"协议转发"一层

```
PM660L GPIO ──SPMI──> 我们的驱动   ✅ 已验证（VOL_UP/VOL_DOWN 打印过）
我们的驱动 ──SimpleTextIn──> ConSplitter ──> ConIn ──> UniXNU   ❌ 卡在这里
```

### 下次的三个技术手段（按优先级）

1. **把日志写进 FAT 卷**（最高优先）
   R11BootRamdisk 已经把 payload 卷挂在 `R11ESP` 上，我们的驱动可以追加写
   `\keys.log`，刷完用 `mtype -i payload.img ::/keys.log` 直接读，不必拍照。

2. **改 UniXNU 源码**（我们有 `src/UniXNU`）
   让它绕过 ConSplitter，直接用 `LocateHandleBuffer(ByProtocol, SimpleTextIn)`
   逐个 `ReadKeyStroke`，或提供一个自定义协议给 UniXNU 认。

3. **在 QEMU 里模拟按键**
   QEMU 的 GOP/ConSplitter 与真机同源，可先用 `qmp` 注入按键验证 UniXNU 侧逻辑。

### 当前可用的镜像

| 文件 | 说明 |
|---|---|
| `out/r11/boot/boot-ui-clean.img` | 界面干净版（推荐日常使用） |
| `out/r11/boot/boot-keys-v3.img` | 带按键驱动（SPMI 读取待恢复） |
| `out/r11/boot/boot-keys-n.img` | 按键驱动 + 去抖，菜单稳定 |

刷机与恢复命令见 3.5 节。

## 3.29 按键：SPMI 观察者通道被拒绝（2026-10-25 17:50）

### 设备树核查（9 个 DTB 全部检查）

所有 9 个 FDT 里音量键都指向 **PMIC GPIO**（不是 SoC 的 TLMM）：

```
fdt1: pm660l-gpio 8/5      fdt6: 3/5
fdt2/7/8: 8/5              fdt3/4/5/9: 8/5
TLMM（pinctrl@03000000, reg = <0x3000000 0xc00000>）phandle = 0x7d，没有按键引用它
```

### 真机扫描结果

| 探测 | 结果 |
|---|---|
| arbiter 版本（core+0x0） | `0x30000000`（v3）✔ 可读 |
| 通道 0x53 的 TYPE（reg 0x04） | `0x01` = PON ✔ 可读 |
| 通道 0x53 的 reg 0x0A/0x0B | `FF`（超时/被拒绝） |
| 512 通道 × 16 寄存器全量快照对比 | **没有任何值随按键变化** |
| 找到的 GPIO(0x10) 外设数 | 0 |
| 找到的 PON(0x01) 外设数 | 0（但 0x53 明明能读 TYPE=0x01 → 说明扫描时多数事务失败） |

### 结论

arbiter v3 的观察者通道**只允许访问少数外设**；我们对 GPIO 外设的读事务被拒绝
（大概率返回 STATUS_DENIED）。redwood 那份参考实现是 **v5** 的布局，
其"观察者通道可读任意外设"的前提在 v3 上不成立。

### 下一步（下次做，按可行性排序）

1. **给 R11SpmiRead 加了 STATUS 日志**：把 `STATUS` 寄存器的原始值画到浮层上，
   确认到底是 DENIED(bit2) 还是 FAILURE(bit1)。
   如果是 DENIED → 需要先在 `cnfg` 区（0x0800A000）把自己写成该通道的 owner。
2. **按 Linux `drivers/spmi/spmi-pmic-arb.c` 的 v2/v3 分支重写访问流程**
   （该文件有 `pmic_arb` 与 `pmic_arb_ver5` 两套；我们用错了 v5 的偏移）。
3. 或者：**放弃按键**，UniXNU 靠超时自动启动（功能已可用），
   把精力放到"内核日志上屏"和"Data seed"上。

### 本轮新增的调试设施（很有价值，以后都能用）

* **屏幕浮层**（`R11DrawOverlay`）：在屏幕最底部两行实时显示
  `CH/A/K/F` + `S/V/P/G`，每 50ms 重画，不会被日志滚掉
* **学习模式**：按住按键 → 对 512 通道 × 16 寄存器做两次快照 → 找变化的位
* 内置 5x7 点阵字体（0-9 A-Z），用 GOP Blt 逐像素画字

## 3.30 内核日志上屏：定位到 gc_enabled，但补丁位置错了（2026-10-25 18:30）

### 已确认的调用链

```
console_write_unbuffered
  → _cnputc
    → cons_ops[1].putc = vcputc_options   (0xFFFFFFF0073B787C)
       开头：
         gc_initialized = [0xFFFFFFF007B2EE00]
         gc_enabled     = [0xFFFFFFF007B2EE04]
         if (gc_initialized == 1 && gc_enabled == 0) → return   ← 什么都不画
       正常路径：
         cbz  gc_buffer_size(0xFFFFFFF007B2F08C) → 跳过
         bl   vc_paint_char (0xFFFFFFF0073BA878)                ← 真正画字
```

### 关键符号

```
_gc_initialized      0xFFFFFFF007B2EE00   （__DATA，zerofill，文件里改不了）
_gc_enabled          0xFFFFFFF007B2EE04   （同上）
_gc_buffer_size      0xFFFFFFF007B2F08C
_gc_enable           0xFFFFFFF0073BBC40   ← 分配缓冲区、置标志
_vc_paint_char       0xFFFFFFF0073BA878
_vcputc_options      0xFFFFFFF0073B787C
```

### 试过但失败的补丁

| 补丁 | 位置 | 结果 |
|---|---|---|
| `b.eq` → `nop`（强制 gc_ops 赋值块） | 0x73BA278 | `vc_paint_char` 仍未调用 |
| `b.ne` → `b`（强制走绘制主流程） | 0x73B78B4 | 无效 |
| 在提前返回处插 `bl gc_enable` | 0x73B78B8~C8 | **内核死锁**（控制台锁已被持有） |

### 教训

`vcputc_options` 在**持锁**上下文里执行；在它内部调用 `gc_enable`
（需要分配内存、可能再取锁）会死锁。轨迹证据：`early_block` 执行了、
`gc_enable` 执行了，但 `main_path`/`vc_paint_char`/`processor_idle` 全都没出现。

### 下次的正确位置

在**启动早期、无锁**的地方调一次 `gc_enable(TRUE)`：
* `initialize_screen` 正常返回的路径（它末尾就是 `b gc_enable`，w0=1）
* 或 `PE_init_console` 之后
* 绝不能在 `vcputc_options` 里面

### 本轮新增脚本

```
scripts/patch-vcputc.py            强制走绘制主流程（已证明无效）
scripts/patch-gc-enabled.py        改 gc_enabled（发现是 zerofill，文件改不了）
scripts/patch-gc-enable-call.py    在 vcputc_options 里调 gc_enable（导致死锁）
```

### QEMU 调试闭环（今天最大的工程收益）

```
改补丁 → qemu-run-r11.sh <kernel> runNN → grep 轨迹/screendump → 30 秒出结论
```

不用刷机即可验证内核补丁对错，下一步继续用这个方法快速试错。

## 3.31 里程碑：内核日志成功显示在真机屏幕上（2026-10-25 18:35）

### 根因与修法（经过完整验证）

`vcputc_options`（0xFFFFFFF0073B787C）开头：

```asm
gc_initialized == 1 && gc_enabled == 0  →  直接 return，一个字都不画
```

运行时实测：

```
_gc_initialized (0xB2EE00) = 1
_gc_enabled     (0xB2EE04) = 0     ← 就是这个导致控制台"禁用"
_gc_buffer_size (0xB2F08C) = 0     ← 缓冲区从未分配
_vc_geometry    (PE_state.video) 全部正确（base/width/height/depth/op=1）
```

### 失败的尝试（都记录在案，别再走）

| 补丁 | 位置 | 结果 |
|---|---|---|
| `b.eq` → `nop`（强制 gc_ops 赋值块） | 0x73BA278 | 无效（那块只写 update_color + 置 gc_initialized） |
| `b.ne` → `b`（强制绘制主流程） | 0x73B78B4 | 无效 |
| 在提前返回处插 `bl gc_enable` | 0x73B78B8 | **死锁**（控制台锁已持有） |
| 在 `PE_init_kprintf` 里调 `gc_enable` | trampoline | 无效（vinfo 还没填）+ 首次实现忘了保存 x30 → 内核不返回 |
| 数据补丁写 `gc_enabled = 1` | 0xB2EE04 | 做不到（__DATA 是 zerofill，文件里没有） |

### 成功的方法

**在 `initialize_screen` 的主 epilogue（`0x73BA7C8: ret`）改成跳到 trampoline**：

```asm
; initialize_screen 末尾
0x73BA7C8:  b  trampoline

; trampoline（写入 __TEXT_EXEC 的 NOP 空洞 0x79E9BEC）
    stp  x29, x30, [sp, #-16]!
    mov  w0, #1
    bl   gc_enable            ; 分配图形控制台缓冲区
    ldp  x29, x30, [sp], #16
    ret
```

**为什么这个时机对**：
* `initialize_screen` 由 `PE_init_platform` 单线程调用
* 此时 `PE_state.video` 已完整填充（base/width/height/depth/op）
* **没有持有任何控制台锁**（之前的死锁就是因为在持锁路径里调）

### 验证（QEMU）

```
vc_paint_char 执行 ✅
gc_enable 执行 ✅
processor_idle 到达 ✅（71538 个基本块，内核健康）
屏幕：完整内核日志
```

### 验证（真机 OPPO R11）

```
Darwin Image4 Validator Version 6.3.0
calling policy_init for AppleImage4
Security Policy loaded: Apple Mobile File Integrity (AMFI)
AppleARMRNSMFantast: Starting
Registered CoreAnalyticsHub functions with xnu.virtual boot
Skylake: sk_copy_64_16 ...... PASSED
...（全部内存自检 PASSED）
IOSurfaceRoot: installMemoryRegions()
DarwinNVRAMHandler.cpp: sync: 894 - serializeVariables failed
Waiting on <dict ID="0"><key>IOProviderClass</key><string ID="1">IONetworkMedia
```

**内核日志第一次显示在 OPPO R11 屏幕上。**

### 关键脚本

```
scripts/patch-init-screen-gc.py    ← initialize_screen 返回前调 gc_enable(TRUE)
scripts/patch-payload-full.py      ← 一次给 payload 里的内核打全套补丁
scripts/find-xrefs.py              ← 扫描 bl 调用点（找 callers）
scripts/find-code-cave.py          ← 找 NOP 空洞放 trampoline
```

### 调试方法沉淀

有了内核日志之后，后续所有问题都可以直接看屏幕：
* 驱动加载顺序
* 每条 Error
* 最后卡在哪个子系统

下一步应该看日志**末尾**停在哪里（IOFindBSDRoot / bsd_init）。

---

## 3.32 根文件系统挂载成功：Data seed → md0 → APFS（2026-10-25 夜）

### 结果（真机 OPPO R11 屏幕日志）

```
Added memory device md0/rmd0 (03000000/0E000000) at FFFFFFFF008570000 for 0000000030000000
BSD root: md0, major 3, minor 0
spec_open : si_devbsdunit initialized to (0), throttle_mask is (0x0), issd is (0)
Serial keyboard started
vm_free_delayed_pages: initialized 0 free pages
VM Swap Subsystem is ON
No POST boot-arg set.
Load_init_program: attempting to load /usr/appleinternal/sbin/launchd.development
```

含义：XNU 已把内存块设备 **md0** 选为 BSD 根设备（APFS 挂载完成），并进入
`Load_init_program` 阶段开始查找/加载第一个用户态程序。

### 关键结论：三条容易踩的坑

| 现象 | 真因 | 解法 |
|---|---|---|
| 内核一直 `Waiting on <dict ... IONetworkMedia>` / `... IOMedia UUID=...` | `/chosen/root-matching` 的 UUID 与任何卷都不匹配；`--packed-data-ramdisk` 写的是 `PackedRAMDisk`，而 **XNU 只认 `RAMDisk`**（`IOKitBSDInit.cpp:766`） | 用稠密 raw APFS + `--data-ramdisk`（ADT 里生成 `RAMDisk` 条目），或者更彻底：**boot-arg `rd=md0`**，走源码里 `mdevlookup()` 的直接内存盘分支，完全绕开 UUID 匹配 |
| DarwinBoot `boot refused: Out of Resources` | 48 MB ramdisk + 54 MB 内核 + 内核窗口，在 2 GB 内存下分配失败 | 真机 4 GB 无碍；QEMU 需 `-m 4096` |
| UniXNU 菜单停在 `timeout -1 ms, input yes` | `Menu.c:1158` `HasInput = (CountInputDevices() > 0)`，有输入设备就永远等按键（QEMU 的串口也算输入设备） | 真机本就没有可用 ConIn，会自动启动；QEMU 调试时可 patch `HasInput = FALSE` + `RemainingMs = 1500` |

### Data seed 的构造与打包

* 生成：`scripts/make-data-seed.sh`（macOS `hdiutil`）
  * `hdiutil create -size 48m -fs APFS` → 写入最小目录 + `com.apple.purplebuddy.plist`
  * `hdiutil convert -format UDRW` 得到**稠密 raw**（48 MB，XNU 内置 ramdisk 路径必须用它）
  * 同时打印并写出 APFS **卷 UUID**（`<out>.uuid`）
* 安装 init（本次新增）：把 `seed/init.macho` 复制到
  `/sbin/launchd`、`/usr/appleinternal/sbin/launchd.development`、`/usr/libexec/launchd`，权限 0755
* 身份：APFS 有两个 UUID —— 容器 UUID 与卷 UUID 不同；`root-matching` 要的是**卷 UUID**

### payload / 固件参数（已确认可用）

| 项 | 值 |
|---|---|
| payload 卷大小 | `board.toml [payload] size_mib = 128` |
| 固件解压缓冲 | `R11_DECOMPRESS_SIZE / R11_MAX_VOLUME = 160 MiB` |
| payload 内容 | `kernel.macho`(54 MB) + `kernel.macho.adt` + `scratch-data.img`(48 MB 稠密 raw) |
| `unixnu.toml` | `initrd = "Payloads/scratch-data.img"`（xnu 类型不打印 initrd 日志，属正常） |
| 内核 boot-arg | 在原 args 后追加 **`rd=md0`** |
| ADT | `make-r11-adt.py ... --data-ramdisk --root-match-volume-uuid <卷UUID>` |

本地打包工具（不需要 macOS）：

```
scripts/repack-payload-bigger.py   ← 重建 128 MiB payload + 换 ADT + 放稠密 seed + 改 toml
scripts/local-add-data-seed.py     ← 只替换 seed/ADT（payload 已够大时用）
```

### QEMU 调试链（本次重大收获）

```
scripts/qemu-run-r11.sh <kernel.macho> <前缀> <seed.raw>
scripts/make-qemu-esp.py   ← 支持 --initrd，生成带 initrd 的 toml
scripts/ppm2png.py         ← 纯标准库把截屏 PPM 转 PNG（无 PIL 也能看图）
```

要点：
* QEMU 需 `-m 4096`，否则 DarwinBoot 因内存不足拒绝启动
* 去掉 `usb-kbd` 并 patch `HasInput = FALSE`，菜单才会自动启动
* 内核日志只在 framebuffer 上，串口到 `exiting boot services` 就结束 → 用截屏看

### 最小 ARM64 Mach-O init（用户态验证工具）

```
scripts/make-minimal-init.py   ← 手写指令直接生成 Mach-O（无需 SDK/assembler）
scripts/verify-macho.py        ← 静态校验：头/命令/段/节/入口/反汇编/字符串
seed/init.macho                ← 573 B，已通过全部检查
```

行为：`open("/dev/console", O_WRONLY)` → `write(fd, "R11_EL0_INIT_OK\n", 16)` →
`write(1, ...)` → `b .` 死循环（避免 PID 1 退出导致 panic）。

XNU arm64 系统调用约定：`x16` = 调用号，`svc #0x80`，出错置 carry（`b.cs` 跳过）；
`SYS_exit=1, SYS_write=4, SYS_open=5`。

### 已知问题 / 下一步

* **用户态执行尚未验证**：`launchd` 不存在于最小 seed，内核会加载失败
  → 已把最小 init 装进 seed，待重新生成 seed 后在 QEMU 里验证 `R11_EL0_INIT_OK`
* **APFS 是否以"完整可读写"方式挂载未验证**：目前只证明内核选定了 md0 作为根
* **真机持久存储驱动缺失**：eMMC（SDHCI）驱动未实现，System 卷（iOS 用户态，数十 GB）
  无法放置 → 真正跑起 launchd 必须先做存储驱动
* 完整 iOS 用户态还依赖 dyld、共享缓存、系统库与大量平台服务，不是单个二进制能解决的

---

## 3.33 EL0 用户态验证：卡在 init exec 被拒（2026-10-25 21:15）

### 已确定的事实链

| 步骤 | 证据 |
|---|---|
| Data seed 交给内核 | `Added memory device md0/rmd0 (03000000/0E000000) ... for 0x3000000` |
| APFS 成为根 | `BSD root: md0, major 3, minor 0` |
| 内核初始化完成 | `VM Swap Subsystem is ON` / `Serial keyboard started` |
| 开始加载 init | `Load_init_program: attempting to load /usr/appleinternal/sbin/launchd.development` |
| exec 失败且内核进入调试器死等 | QEMU gdb：PC 落在 `_panic_trap_to_debugger+0x2fc` 的 `wfe` 循环 |

**结论：内核选定了 md0 作为根并尝试 exec `/usr/appleinternal/sbin/launchd.development`，
但 exec 未成功，随后 panic → 进 `panic_trap_to_debugger` 自旋。**

### init 的候选路径（源码确认，kern_exec.c:7104-7109）

```
/usr/appleinternal/sbin/launchd.debug        (仅 DEBUG)
/usr/appleinternal/sbin/launchd.development  (DEVELOPMENT || DEBUG)
/sbin/launchd                                (所有构建)
```

DEVELOPMENT 内核里 `launchdsuffix` boot-arg 可以覆盖第一项；
全部失败后 `panic("Process 1 exec of %s failed, errno %d")`（kern_exec.c:7197）。

### 最小 init 程序（已通过静态验证）

```
scripts/make-minimal-init.py   → seed/init.macho (573 B)
scripts/verify-macho.py        → 头/命令/段/节/入口/反汇编/字符串 全部 OK
```

* 手写 18 条 ARM64 指令，无 SDK：`open("/dev/console")` → `write` 标记串 → `write(1)` → `b .`
* `LC_SEGMENT_64 __TEXT`（vmaddr 0x100000000，fileoff 0，RX）+ `LC_UNIXTHREAD`（ARM64，pc=入口）
* 内核侧确认会构造 argv/envp 栈（`execve(p, &init_exec_args)`），所以 sp 不用我们设

### 排掉的可能性

| 假设 | 排除依据 |
|---|---|
| 内核内存不足 | QEMU 提到 4 GB 后 DarwinBoot 交接成功 |
| init 文件不在 seed 里 | CI 日志显示三条路径都已写入 573 B |
| 路径写错 | `Load_init_program` 打印的正是我们放的路径 |
| 我们误判为"卡死" | 曾以为是死锁；gdb 显示实际停在 panic 调试器自旋 |

**调试陷阱（重要）**：QEMU 配置里原来的 `debug=0x8` 是 **DB_KDB**（内核调试器），
一旦异常就进 `wdt` 式自旋且不打印，掩盖了真正的 panic 文本。已改为
`-v serial=3 keepsyms=1 rd=md0`。

### 尚未排除的三条假设（按可能性排序）

1. **AMFI / 代码签名拒绝**：ADT 里设了 `amfi-only-platform-code=1`，我们的 init 无签名、
   也不在信任缓存里。这次加了 `amfi_get_out_of_my_way=1 cs_enforcement_disable=1` 仍未执行，
   但这两个 boot-arg 在 iOS 17 系内核上大概率已失效 → 需要**真正的信任缓存或 ad-hoc 签名**。
2. **Mach-O 结构仍缺项**：例如需要 `__PAGEZERO`、`LC_MAIN`，或内核要求 `__LINKEDIT`。
   （`kern_exec.c` 只校验 `MH_EXECUTE` + 架构 grade，但 `load_machfile` 内部还有段布局检查）
3. **静态（无 dyld）二进制在 arm64 iOS 上不受支持**：需要 `LC_LOAD_DYLINKER`。

### 下一步（按成本从低到高）

| # | 实验 | 成本 |
|---|---|---|
| 1 | 用 `load_machfile` 的返回码定位：在 QEMU 里用 gdb 设断点在内核 exec 返回处，直接读 x0 | 低（需 gdb 符号） |
| 2 | 给 init 加 `__PAGEZERO` + `LC_MAIN` 变体，看行为是否变化 | 低 |
| 3 | 生成 ad-hoc 代码签名（CodeDirectory + LC_CODE_SIGNATURE）并做信任缓存 | 中 |
| 4 | 在 QEMU 里对 exec 路径打日志补丁（内核侧 printf 到屏幕） | 中 |
| 5 | 换真机验证（真机 4 GB，AMFI 行为可能与 QEMU 不同） | 低但占用设备 |

### 已知问题清单

* 用户态执行**尚未证实**（`R11_EL0_INIT_OK` 未出现）
* 无法读取 panic 文本：`panic_info`/`debug_buf` 全为 0（内核未启用 debug buffer），
  `msgbuf` 无符号；panic 打印似乎未走到我们 patch 出来的图形控制台
* 真机持久存储（eMMC）驱动仍未实现 → 无法放置 iOS 用户态

---

## 3.34 EL0 验证的完整进展（2026-10-25 23:00）

### 今晚新确认的事实（按证据强度排序）

| # | 结论 | 证据 |
|---|---|---|
| 1 | 内核**必须**打 PAN 补丁才能在 ARMv8.0 上跑用户态 | `_copyout+0x5c` 是 `msr PAN,#0`（ARMv8.1），A72/A73 无此指令 → sleh panic「Undefined kernel instruction: pc=... instr=d500409f」；改 nop 后正常 |
| 2 | MAC/AMFI **放行**我们的 init | gdb：`mac_vnode_check_exec` 返回 0 |
| 3 | `exec_mach_imgact` 返回 **-1** | gdb 在调用者返回点读到 x0=0xffffffff |
| 4 | -1 来自 **filetype 检查** | 反汇编 `0x76cca78: ldr w8,[x24,#0xc]; cmp w8,#2; b.ne → mov w0,#-1` |
| 5 | 我们的文件头本身**完全合法** | seed 里 magic×3、`02 00 00 00`(filetype=2)@偏移12、sizeofcmds=440 全部正确 |
| 6 | panic 时用的是 `/sbin/launchd` 路径 | panic 参数区 sp[0]=VA→文件偏移 0xbf020 = `"/sbin/launchd"`，说明三条候选都失败 |

**矛盾点（下次要解的）：** 文件里偏移 12 明明是 2，内核读到的缓冲区却让 `cmp w8,#2` 失败
→ 必须 dump 内核的 `x24`（头缓冲区）才能定性：全 0（读失败）/ 别的文件 / 其它。

### 已完成的修复（真机与 QEMU 都适用）

```
scripts/patch-kernel-pan.py     9 处 msr PAN → nop（copyin/copyout/pmap_test_access）
```

`/tmp/k-pan.macho` 是打好补丁的内核；真机 payload 里的 kernel.macho 也需要打同样的补丁
（可直接调用该脚本，或并入 patch-payload-full.py）。

### 调试工具链（本轮新增，全部可复用）

```
scripts/qemu-run-r11.sh      CATCH_PANIC=1 / CATCH_EXEC=1 两种 gdb 捕获模式
scripts/analyze-inasm.py     把 -d in_asm 轨迹映射到内核符号（定位 panic 前调用链）
scripts/qmp-regs.py          QMP 读 CPU 寄存器
scripts/ppm2png.py           纯标准库 PPM→PNG
scripts/make-minimal-init.py 手写 ARM64 Mach-O（573 B）
scripts/verify-macho.py      静态校验（头/段/节/入口/反汇编/字符串/段长度）
```

### 关键调试经验（避免以后再踩）

1. **`debug=0x8` 会打开 KDB**：一旦异常就静默 wfe，看不到 panic 文本 —— 调试 QEMU 时要删掉
2. **`cs_enforcement_disable=1` 会让 AMFI 直接 panic**（"can't has cs_enforcement_disable"）
3. gdb + `-s` + `hbreak *_panic` 是**拿到 panic 文本最可靠的方式**（x0=格式串、x1..=参数）
4. gdb 不支持 Mach-O 符号 → 先用 `llvm-nm` 取地址，再按裸地址下断点
5. `$x30` 等在 `-ex` 里必须用**单引号**，否则被 bash 展开
6. 二进制 patch 脚本必须校验读到的内容非空（本轮曾把脚本写成空文件）

### 下一步（明天从这里继续）

1. `CATCH_EXEC=1` 断 `0xfffffff0076cca78`，dump `x24` 指向的 32 字节
   —— 这一条决定「读失败」还是「读到别的文件」
2. 若读到的是全 0 → 查 APFS 读取路径（可能是 seed 的元数据问题）
3. 若是别的内容 → 查路径解析（`/usr/appleinternal/sbin/launchd.development` 是否指向我们的 inode）
4. 若确认全对但检查仍失败 → 在 `ldr w8,[x24,#0xc]` 之后读 `w8` 的实际值

---

## 3.35 关键转折：ENOENT 与 ENOEXEC 是两个独立问题（2026-10-25 23:20）

### 屏幕上终于看到的完整失败链

```
Load_init_program: attempting to load /usr/appleinternal/sbin/launchd.development
Load_init_program: attempting to load /sbin/launchd
Load_init_program: failed loading /sbin/launchd: errno 8
```

按 `kern_exec.c` 的逻辑（非 ENOENT 才打印 "failed loading"）：

| 路径 | 返回值 | 结论 |
|---|---|---|
| `/usr/appleinternal/sbin/launchd.development` | ENOENT | 路径查找失败（文件/目录不存在或不可达） |
| `/sbin/launchd` | ENOEXEC(8) | 找到了文件，但被判定为不可执行 |

已经验证 seed 镜像里**存在**这些名字（`appleinternal`×2、`launchd.development`×2、`launchd.debug`×2），
所以 ENOENT 不是"没写进去"，而是**路径/权限层面**的问题。

### ENOEXEC 的可能来源

`exec_activate_image` 里有一条早于 Mach-O 解析的检查：

```c
/* Disallow zero length files */
if (vap->va_data_size == 0) {
        return ENOEXEC;
}
```

如果 APFS 报告的 `va_data_size` 为 0（或读取失败），就会**直接 ENOEXEC**，
完全不会进入 `exec_mach_imgact` —— 这解释了为什么最后一次断在
`0xfffffff0076cca78`（filetype 检查）**没有命中**。

另一个来源是 `error == -1`（magic/filetype 检查失败）转换为 ENOEXEC，
之前一次运行确实观测到 `exec_mach_imgact` 返回 -1。

### 下次继续的两条命令

1. 断 `_exec_mach_imgact` 入口（0xfffffff0076cc9d0），确认这一次是否被调用：
   * 命中 → 问题在 Mach-O 解析（继续 dump `x24` 指向的头 32 字节）
   * 不命中 → 问题在 size==0 或权限检查（查 APFS 报告的 `va_data_size`）
2. 若怀疑 size==0：在 `exec_activate_image` 的 size 检查处下断点，读 `vap->va_data_size`

### 修复优先级

| # | 动作 | 目的 |
|---|---|---|
| 1 | 用 boot-arg `launchdsuffix=release` 强制只试 `/sbin/launchd` | 排除路径差异，聚焦单一失败点 |
| 2 | seed 里改用 `chown 0:0` + `chmod 0755`，并显式 `sync` 后再 detach | 排除 size/权限元数据问题 |
| 3 | 在 CI 里 `hdiutil attach` 后 `ls -l` 打印三个路径的**大小与权限** | 直接确认文件元数据 |

---

## 3.36 APFS 文件数据读取返回 0 字节（2026-10-25 23:55）

### 决定性观测

在 `exec_mach_imgact` 的 `ldr x24,[x0,#0x40]` 之后 dump 头缓冲区，两次（573 B 版与 64 KB 填充版）结果相同：

```
0x…:  b4 a3 4c 5a 7f 5c 36 a8   01 00 00 00 00 00 00 00
      04 00 00 00 00 00 00 00   01 00 00 80 00 00 00 00
```

解出来是 **APFS obj_phys_t**：`cksum(8)` + `oid=1` + `xid=4` + `type=0x80000001`（OBJ_PHYSICAL|NX_SUPERBLOCK），
也就是**容器超级块（磁盘第 0 块）**。两次只有 checksum 不同（镜像不同）。

结论：**内核读文件内容时返回 0 字节**，缓冲区残留挂载早期读过的 NXSB；
于是 magic 检查失败 → `error = -1` → **ENOEXEC**。

### 已排除

| 假设 | 排除依据 |
|---|---|
| 文件未写入 seed | 镜像里有 3 份 Mach-O（magic ×3、标记串 ×3） |
| 文件是 APFS 内联存储 | 三份数据都在**块首对齐**位置（0xa7000/0xb7000/0xc7000），各占 16 个块 |
| 权限问题 | seed 日志显示 `-rwxr-xr-x`，目录也 chmod 755 |
| 目录不存在 | `/sbin/launchd` 能打开（否则 ENOENT） |

### 仍存疑

* `exec_activate_image` 有一条 `if (vap->va_data_size == 0) return ENOEXEC;` —— 如果 APFS 报 size=0，
  内核**根本不会**读内容。这与"栈里残留 NXSB"完全一致，需要直接读 `vap->va_data_size` 验证。
* 另一个可能：读内容时 APFS 返回错误（inode 的 dstream/extent 记录与本内核 APFS 版本不兼容）。

### 下一步（明天按此顺序）

| # | 实验 | 判定 |
|---|---|---|
| 1 | 断 `exec_activate_image` 里 size 检查处，读 `vap->va_data_size` | =0 → 是 inode 元数据问题；=65536 → 是读内容失败 |
| 2 | 用 **HFS+** 做根（Linux `mkfs.hfsplus` + `hfsplus` 写文件） | 若 init 能跑 → 说明整条 exec 链路 OK，问题纯在 APFS 数据读取 |
| 3 | 换一种造 seed 的方式（`diskutil apfs create`、或从真 iOS 设备 dump 一个最小 Data 卷） | 版本兼容性验证 |

### 今日总进度

```
✅ 自制 UEFI 固件启动
✅ UniXNU 启动管理器
✅ DarwinBoot 交接 XNU
✅ XNU 完整启动 + 日志上屏
✅ APFS Data 卷挂载为根（真机 + QEMU）
✅ PAN 补丁（ARMv8.0 跑用户态的前提）
✅ 排除 AMFI / Mach-O 格式 / 内存不足
⬜ APFS 读文件内容返回 0 → init exec 失败（唯一剩余阻塞点）
```

---

## 3.37 定位完成：镜像正确，内核 APFS 解析错误（2026-10-26 01:40）

### 决定性对比

| 检查者 | 结果 |
|---|---|
| Linux `fsapfsmount`（libfsapfs） | `/sbin/launchd` 65536 B, 0755, root:root, inode 5 — **完全正确** |
| 内核 APFS（v2236） | `vap->va_data_size = 0x3000000`（= 容器 block_count×block_size），读取内容得到块 0（NXSB） |

**结论：镜像与文件都没有问题，是内核侧 APFS 驱动解析本卷 inode 时出错。**
`va_data_size = nx_block_count × nx_block_size` 这个巧合是铁证：
驱动把 inode 的读取位置解析成了 0 号块，于是"inode"内容就是容器超级块本身。

### 已排除（本轮全部有实证）

* 文件未写入 / 权限 / 目录缺失 → fsapfsmount 可读
* APFS 内联存储 → 数据在块首对齐的普通块里
* 零长度文件（exec_check_permissions 的 ENOEXEC 分支）→ 实际 size 是 48 MB，不是 0
* exec 对象是目录（会返回 EACCES）→ 实际返回 ENOEXEC，说明是普通文件
* AMFI（`mac_vnode_check_exec` 返回 0）
* HFS（内核无 HFS 驱动符号）
* mockfs（存在，但只建 ROOT/DEV，无 /sbin/launchd）

### 可疑点：卷特性版本

```
fsapfsinfo: Compatible features 0x2 = NX_FEATURE_LCFD
```

本卷由 **macOS 15 runner** 创建，而内核是 **iOS 17 的 apfs-2236**。
特性集差异是当前最合理的解释方向。

### 下一步实验（按成本排序）

| # | 动作 | 判定 |
|---|---|---|
| 1 | CI 改用 `macos-13` 造 seed，再 `fsapfsinfo` 对比特性位 | LCFD 消失 → 假设成立 |
| 2 | 用 `newfs_apfs` 显式参数造卷（若能关闭新特性） | 同上 |
| 3 | 在真机 Data 分区上截取一个最小镜像作为种子 | 最贴近目标环境，但需要设备 |
| 4 | 内核侧：给 apfs kext 打补丁/换版本 | 工作量大 |

### 本轮新增工具

```
fsapfsmount / fsapfsinfo（libfsapfs-utils）—— Linux 侧独立验证 APFS 镜像
scripts/qemu-run-r11.sh 的 CATCH_EXEC 模式 —— dump imgp / vnode / 头缓冲区
```

---

## 3.38 特性版本假设被否定；问题收敛到 APFS kext 读取行为（2026-10-26 03:00）

### 实验记录

| 实验 | 结果 | 结论 |
|---|---|---|
| macOS 15 造卷 | `Compatible features: 0x2 (NX_FEATURE_LCFD)` | 基线 |
| macOS 14 造卷 | 同样 `0x2 (NX_FEATURE_LCFD)` | **特性不是差异点**，假设否定 |
| Linux fsapfsmount 读卷 | `/sbin/launchd` 65536 B / 0755 / inode 5，`/usr/appleinternal/sbin/*` 两份都在 | **镜像完全正确** |
| 内核 exec 我们的 init | `va_data_size = 0x3000000`（= 容器 block_count×block_size），读内容 = 块 0 | **驱动侧解析错误** |

### 当前最可能的技术解释

1. APFS 挂载时读了大量块（容器/卷超级块/B-tree）都正常 → **带偏移的读本身是好的**
2. 但读 `/sbin/launchd` 的 inode 时得到的是块 0 的内容（NXSB）
   → 该 inode 的读取地址被解析成 0（extent 记录或 omap 解析出错）
3. `/usr/...` 路径返回 ENOENT（文件确实存在）
   → 路径解析层也不正常，可能与 XNU 的 firmlink/synthetic 目录处理有关

### 剩下的可选方向（下次择一）

| # | 方向 | 成本 | 风险 |
|---|---|---|---|
| A | 在 `-d in_asm` 轨迹里找 APFS kext 段（`0x4axxxxx`）执行的读块调用，看它请求的块号 | 中 | 需要反汇编 kext 段 |
| B | 造一个更小的卷（如 8 MB）测试是否与容量/偏移计算相关 | 低 | 可能无信息 |
| C | 用真 iOS 设备 dump 的最小 Data 卷作为种子 | 中 | 需要设备 |
| D | 放弃 APFS 作为 EL0 验证载体；改为在 payload 里放一个**内核可直接执行的 Mach-O**（需要内核补丁，如 hook `load_init_program`） | 高 | 侵入性强 |
| E | 检查 APFS kext 的 block-size 假设（512 vs 4096）是否与 md0 的 IOMedia 一致 | 中 | 需要看 kext 与 md 驱动的交互 |

### 关键资产清单（下次直接可用）

```
镜像:  seed5/seed.raw (macOS 14 版, UUID 980A1373-...) / seed4 (macOS 15 版)
内核:  /tmp/k-pan.macho (PAN nop 补丁)
ADT:   make-r11-adt.py --data-ramdisk --root-match-volume-uuid <UUID>
工具:  qemu-run-r11.sh (CATCH_PANIC / CATCH_EXEC), analyze-inasm.py, ppm2png.py,
       fsapfsinfo/fsapfsmount, verify-macho.py, make-minimal-init.py --pad
验证:  fsapfsmount 挂载镜像可读全部三份 init（镜像侧确认）
```

### 已确认成立的上游事实（不需重复验证）

* `rd=md0` 让内核把内存盘当根（绕开 IOMedia UUID 匹配）
* `msr PAN` 的 9 处必须 nop，否则 ARMv8.0 上任何用户内存访问都 panic
* `mac_vnode_check_exec` 放行（AMFI 不是阻塞点）
* exec 的对象是**普通文件**（否则返回 EACCES 而不是 ENOEXEC）
* 内核无 HFS；mockfs 存在但只有 ROOT/DEV 节点

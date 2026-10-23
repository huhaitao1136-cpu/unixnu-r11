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

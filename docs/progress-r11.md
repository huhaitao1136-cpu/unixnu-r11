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

---

## 3.39 卷属性假设排除；HFS 确认不存在（2026-10-26 03:50）

### 本轮实验与结果

| 实验 | 结果 | 结论 |
|---|---|---|
| 卷名改 `OwnerData` + 32 MB（对齐官方 fixture） | `fsapfsinfo` 显示 `Name: OwnerData` ✓ | 卷名生效 |
| 同上，QEMU 断点 dump 头缓冲区 | 仍是 APFS obj_phys（NXSB 结构） | **卷名/尺寸都不是原因** |
| 搜索内核 HFS 支持 | `Apple_HFS` 仅出现在 IOMedia 匹配字典与分区类型表；无 hfs_vfsops | **内核无 HFS 驱动** |
| `fsapfsmount` 复核 | 三份 init 均可读、65536 B、0755 | 镜像侧无问题 |

### 当前唯一阻塞点（已收敛到最小范围）

```
内核 APFS 读 /sbin/launchd 的 inode 时拿到块 0 的内容（NXSB）
  ⇒ 文件大小被解析成 nx_block_count × nx_block_size = 48 MB
  ⇒ magic 检查失败 → error = -1 → ENOEXEC → 三个候选全失败 → panic
```

### 下次的两条路径

| # | 路径 | 说明 | 成本 |
|---|---|---|---|
| A | **拿 Apple 自制的 APFS 镜像**当 Data seed（IPSW 里的 RestoreRamDisk / OS DMG 提取） | 若 Apple 卷能读 → 证明是"我们卷的某种结构"问题；若也不能读 → 证明是内核 APFS 与我们的 md0 不匹配 | 中（需下载几 GB IPSW） |
| B | **反汇编 APFS kext 段**（`0x4axxxxx`），定位 fs-tree 节点读取路径 | 最彻底，能直接看到块号为何变成 0 | 高 |

### 已充分验证、无需再试的结论

* `rd=md0` + 稠密 raw APFS（`--data-ramdisk`）能让内核挂载 Data 卷为根（真机 + QEMU）
* 9 处 `msr PAN` 必须 nop（ARMv8.0 无 PAN），否则任何用户内存访问都 panic
* AMFI 放行（`mac_vnode_check_exec` 返回 0）
* exec 对象是普通文件（EACCES/EACCES 区分已做）
* 镜像结构正确（Linux libfsapfs 独立验证）
* 卷名、尺寸、macOS 版本、APFS 特性位（LCFD）都不是差异点

### 今日（10-25 全天）总进度

```
① UEFI 固件               ✅
② UniXNU 启动管理器        ✅
③ DarwinBoot 交接 XNU     ✅
④ 内核完整启动 + 日志上屏   ✅
⑤ APFS 卷挂载为 BSD 根     ✅（真机 md0）
⑥ PAN 补丁（EL0 前提）      ✅
⑦ init exec               ⛔ APFS 驱动读 inode 返回块 0
```

---

## 3.40 补丁矩阵收敛 + 页大小不匹配假设（2026-10-26 23:30）

### 补丁必要性矩阵（QEMU，Apple ramdisk 作为 initrd）

| 变体 | trap 修复 | mrs PAN | CPU 校验 | PAN nop | 结果 |
|---|---|---|---|---|---|
| full | ✓ | →1 | ✓ | ✓ | 启动成功，APFS 读文件失败（ENOEXEC） |
| A | ✗ | ✗ | ✓ | ✓ | zone_init 阶段 panic |
| C | ✓ | ✗（未改） | ✓ | ✓ | `Undefined kernel instruction: instr=d5384268`（mrs PAN） |
| D | ✓ | →0 | ✓ | ✓ | `panic: context switch with PAN disabled` |
| E | ✓ | →1 | ✗ | ✓ | `panic: CPU configuration in device tree is invalid` |

**结论：`patch-kernel-full.py` 的补丁集已经是最小必需集**
（PAN nop + mrs PAN→1 + 0xE7FFDEFF trap→nop + CPU 校验+输出开关+serial_init+cons_ops_index），
APFS 读文件失败与本项目补丁无关。

### 关键新线索：页大小

* 内核由 `scripts/build-xnu-4k-baseline.sh` 构建 → **4 KB 页**
* 启动日志显示 `SKIO: skmem_region_init seg_size 16384`
* APFS kext 来自 Apple 预编译（iOS 上为 **16 KB 页** 假设）

假设：**APFS 的页缓存（ubc）路径按 16 KB 页计算偏移，在 4 KB 内核上错位**，
而元数据走块设备读写（buf_strategy，按 4 KB 块）不受影响。
这解释了"能挂载、能查找、但读不出文件内容"，也解释了 Apple 原版 ramdisk 同样失败。

### 验证该假设的方法

| # | 方法 | 说明 |
|---|---|---|
| 1 | 查内核构建的 page size 配置 | `configs/r11_defconfig` 里 `PAGE_SIZE`/`ARM64_PGSZ` 相关项 |
| 2 | 查 APFS kext 的页大小假设 | kext 二进制里 `_page_size`/`PAGE_SHIFT` 相关常量，或 misalignment 检查 |
| 3 | 用 16 KB 页重新构建内核 | 需要改 UniXNU 的构建设置；CI 内核构建目前仍受阻 |
| 4 | 对比：4 KB 页内核 + 4 KB 页的 APFS kext | 需要 Apple 的 4 KB 版本 kext（不存在于 iOS） |

### 相关资产（本轮新增）

```
scripts/patch-kernel-minimal.py   可分段打补丁：--with-validate / --with-trapfix / --with-mrs0 / --with-mrs1
/tmp/k-vA..vE.macho               五个变体内核（QEMU 已验证各自行为）
/tmp/rrd-apfs.img                 Apple 恢复 ramdisk（186 MB，裸 APFS，含真 launchd）
scripts/fetch-ipsw-member.py      HTTP Range 取 IPSW 单个成员（不用下 6 GB）
```

### 今日结论

```
内核 → 挂载 APFS → 查找文件 → 读取内容   ⛔ 最后一步读到的总是块 0
根因方向：4 KB 内核页 vs 16 KB APFS kext 页假设
```

---

## 3.41 CI 内核构建推进到链接阶段：缺 IOStorageFamily（2026-10-26 23:40）

### 本轮改动与结果

| 改动 | 目的 | 结果 |
|---|---|---|
| defconfig `CONFIG_XNU_NATIVE_RAM_BLOCK=y` | 启用块存储驱动 | 归档成功编译 |
| defconfig `CONFIG_XNU_NATIVE_LEGACY_DISPLAY=n` | 让 bsd_init 不再引用 display probe | ✅ 之前 `audit-mobile-graphics-legacy` 的失败消失 |
| board.toml 注释 `build-native-legacy-display.py` | 同上 | ✅ |
| board.toml 加 `build-arm-driver.py NativeRamBlock` | 启用驱动 | ✅ 编译并进入链接 |

### 链接失败的两类符号

```
1) IOStorageFamily 的实现（来自 libNativeRamBlock.a）
   IOBlockStorageDevice::gMetaClass / init(OSDictionary*) / requestIdle() /
   doUnmap(...) / vtable for IOBlockStorageDevice ...

2) NativeUfs 持久化层（我们的 r11-stubs 缺两个）
   _native_ufs_store_gpt_check
   _native_ufs_store_mode
```

第 2 类已在本轮补进 `package/r11-stubs/ufs-store-stubs.c`（返回 -1）。

第 1 类是**块存储栈的根本缺口**：`prepare-storage-headers.py` 只准备了 19 个公共头，
没有 IOStorageFamily 的实现。这正是 board.toml 早期注释所说的
「第一阶段不启用块存储栈（依赖参考内核候选），改用 mockfs」。

### 打通块存储的两条路线

| 路线 | 做法 | 代价 |
|---|---|---|
| A | 给 IOStorageFamily 做"链接桩"归档：提供 `IOBlockStorageDevice` 的 vtable 与方法符号（返回错误），让内核能链接；prelink 时若 Apple 的 IOStorageFamily kext 提供真实实现则自动覆盖 | 中：需要按 C++ 修饰名精确钉符号 |
| B | 把 `refer/IOStorageFamily` 的真实源码编进内核 | 中～高：需要构建脚本支持与版本匹配 |

### 已确认的上游事实（本轮）

* 内核构建脚本用 `KERNEL_EXTRA_ARCHIVES + -Wl,-force_load` 收集可选归档，
  **没有** ramblock 专属分支 —— 说明 NativeRamBlock 是通过 board.toml 的 archives
  编译后由脚本的通用逻辑链接（本轮已验证它确实被链接）。
* `XNU_NATIVE_LEGACY_DISPLAY`（0/1）控制 bsd_init 是否引用 display probe；
  设为 0 后即可安全移除 legacy-display 归档。
* 内核当前**没有任何块设备驱动**（无 virtio / USB mass storage 符号），
  因此无法在 QEMU 里用真实块设备验证 APFS 读取。

### 下一步（明天）

1. 验证 `r11-stubs` 新增的两个符号让链接错误减少到只剩 IOStorageFamily
2. 为 IOStorageFamily 生成链接桩（路线 A）
3. 链接通过后，ADT 改 `--packed-data-ramdisk`，在 QEMU 里用 Apple ramdisk 验证 APFS 读文件是否恢复

---

## 3.42 块存储栈的硬依赖：native-owner 参考候选（2026-10-27 00:00）

### 本轮 CI 结果

```
✅ legacy-display 归档移除后，之前的 audit 失败消失
✅ 内核补丁(stubs)生效：链接错误里不再出现 _native_ufs_store_gpt_check / _mode
❌ 仍剩 IOStorageFamily：
     IOBlockStorageDevice::gMetaClass / init(OSDictionary*) / requestIdle() /
     doUnmap(...) / vtable for IOBlockStorageDevice / getProperty(...) ...
     ← 全部来自 libNativeRamBlock.a
```

### 依赖链

```
board.toml:  "build-arm-driver.py NativeRamBlock"
   ↓ 需要 XNU_NATIVE_AES=1（已满足）
   ↓ 驱动 import 原始 IOStorageFamily 的类实现
build-xnu-4k-baseline.sh:
   if [ "$NATIVE_RAM_BLOCK" = 1 ] && [ "$XNU_NATIVE_AES" != 1 ]; then exit 1
   ...
   KERNEL_EXTRA_ARCHIVES="$KERNEL_EXTRA_ARCHIVES -Wl,-force_load,$RAM_BLOCK_ARCHIVE"
   ↓
链接时需要 storage 的"原始绑定"
   ↓ 来自 build/native-prelink/native-owner-*.macho
   ↓ 而该文件：
       - audit-*.py / build-native-*.py 都是"读"
       - 没有任何脚本"写"它 → 离线资产（参考设备/kernelcache 探测产物）
```

### 这意味着

* 启用块存储（NativeRamBlock / NativeVirtioBlock）在当前仓库条件下**不可行**，
  除非自己实现 IOStorageFamily 或拿到 native-owner 参考候选。
* 与之一致的历史线索：board.toml 早期注释「第一阶段不启用块存储栈（依赖参考内核候选），改用 mockfs」。

### 另一道墙：APFS on 内置 ramdisk

* 元数据读取正常（挂载成功、目录查找部分可用）
* 文件内容读取返回**块 0**（容器超级块），我们卷与 Apple 原版 ramdisk 表现一致
* 已排除：镜像、卷名、尺寸、特性位、macOS 版本、AMFI、Mach-O 格式

### 下次可选方向

| # | 方向 | 前提 | 评价 |
|---|---|---|---|
| 1 | 自己实现最小 IOStorageFamily（IOMedia + IOBlockStorageDevice），让 NativeRamBlock 有超类 | 需要 4K 版 IOMedia 语义 | 工作量大 |
| 2 | 给内核打"从内存直接加载 init"的补丁（绕过 VFS/APFS） | 需要写 Mach-O 加载逻辑 | 中线，但能立刻证明 EL0 |
| 3 | 继续查 APFS 读块 0 的根因 | 需要反汇编 APFS kext（无符号） | 难 |
| 4 | 拿到 native-owner 参考候选（参考设备探测） | 需要目标设备/离线环境 | 依赖外部资产 |

### 推荐

方向 2：写一个内核侧的最小 Mach-O 加载器补丁（把 ramdisk 里已知偏移的 init 映像直接映射进用户地址空间并设好入口），
可以完全绕开 APFS 的读取路径，直接验证 EL0 —— 与「先进入用户态」的目标一致，且不依赖外部资产。

---

## 3.43 突破：mockfs 让内核读到了我们的 Mach-O（错误码 8 → 86）（2026-10-27 00:40）

### 关键实验

用 **mockfs** 作为根文件系统，ramdisk 内容为裸 Mach-O（我们的 init，偏移 0）：

```bash
python3 -c "from pathlib import Path; src=Path('seed/init.macho').read_bytes(); Path('/tmp/init-rd.img').write_bytes(src)"
# ADT: --data-ramdisk（RAMDisk 条目）
# QEMU: rd=md0，ramdisk = 64 KB Mach-O
```

结果（panic 捕获）：

```
x0: "Process 1 exec of %s failed, errno %d"
sp[0] = 0xfffffff0070c3020   ← 路径字符串 "/sbin/launchd"
sp[1] = 0x56 = 86            ← errno = EBADEXEC
```

### 与之前 APFS 路径的对比

| 根文件系统 | errno | 含义 |
|---|---|---|
| APFS（我们的卷 / Apple ramdisk） | **8** = ENOEXEC | 内核拿到的不是合法 Mach-O（读到块 0） |
| **mockfs + 裸 Mach-O ramdisk** | **86** = EBADEXEC | 内核读到了合法 Mach-O 头，但后续校验拒绝 |

**结论：mockfs 路径完全绕过了 APFS 的读取缺陷，内核已能正确读到我们的可执行文件。**

### EBADEXEC 的可能来源（下一步要查）

| 来源 | 说明 |
|---|---|
| AMFI / 代码签名 | 我们的 init 无签名、不在信任缓存；`EBADEXEC` 是 AMFI 拒绝的典型返回 |
| `exec_mach_imgact` 的静态二进制校验 | 例如要求 `LC_MAIN`、`__LINKEDIT`、或 PIE 标志 |
| mockfs 文件的 size = 设备大小 | exec 可能对"文件大小 64 KB 但段只覆盖 573 字节"有额外检查 |

### 下一步的验证方法（明天）

1. `CATCH_EXEC=1` 断 `exec_mach_imgact`，看返回值与 `ip_arch_size`（此时应等于 65536）
2. 若返回 86 来自 AMFI：再断 `mac_vnode_check_exec`（mockfs 的 vnode 路径）
3. 若来自 Mach-O 校验：对比 Apple ramdisk 里 launchd 的 Mach-O 结构（有无 `__LINKEDIT`、`LC_MAIN`、PIE、签名）

### 今日（10-25 全天）最终进度

```
① UEFI 固件                ✅
② UniXNU 启动管理器         ✅
③ DarwinBoot 交接 XNU      ✅
④ 内核完整启动 + 日志上屏    ✅
⑤ APFS 卷挂载为 BSD 根      ✅（真机 + QEMU）
⑥ PAN 全套补丁（EL0 前提）   ✅（5 个变体验证必需性）
⑦ 内核读到我们的 Mach-O      ✅（mockfs 路径，错误码 8→86）
⑧ EL0 执行                 ⛔ 卡在 EBADEXEC 校验
```

**距"用户态执行"只差最后一道校验。**

---

## 3.44 EBADEXEC 定位：load_machfile 返回 LOAD_FAILURE（2026-10-26 23:50）

### 源码事实

`load_return_to_errno()`（kern_exec.c:7238-7247）：

```c
case LOAD_IOERROR:    return EIO;
case LOAD_DECRYPTFAIL: return EAUTH;
case LOAD_FAILURE:
default:              return EBADEXEC;      /* ← 我们拿到的 86 */
```

`encapsulated_binary` 处也可能产生 EBADEXEC（activator 迭代超过 `EAI_ITERLIMIT`）。

### 推到这一步的证据链（mockfs 通道）

```
ramdisk = 裸 Mach-O (64 KB, 偏移 0)
→ APFS 挂载失败 → mockfs 兜底成为根
→ /sbin/launchd 命中 mockfs 的 file 节点（内容 = 整个设备）
→ 内核 dump 到的头 = 0xfeedfacf 0x0100000c 0x00000002 0x00000002 0x00000002 0x000001b8
  （与我们的 init 文件逐字节一致 ✅）
→ ip_arch_size = 0x10000（设备大小）
→ exec_mach_imgact 通过头部检查
→ 最终 errno = 86 = EBADEXEC ← load_machfile 的 LOAD_FAILURE
```

### 明天的第一步（一条命令）

把 CATCH_EXEC 的断点改回 `load_machfile` 入口 + 返回：

```bash
# 断 _load_machfile = 0xfffffff00775a8a8
# 命中后读 x0(ip) x1(mach_header) x30(返回地址)
# 在 $x30 下断点，continue，读 x0 = load_return_t
```

判读：

| load_return_t | 含义 | 修法 |
|---|---|---|
| LOAD_FAILURE | 段映射失败 | 补 `__PAGEZERO` / `__LINKEDIT`，或调整 `__TEXT` 的 fileoff/vmsize |
| LOAD_NOCPUS | CPU 相关 | 检查 cpusubtype |
| LOAD_CANTCREATE | 内存映射失败 | 检查 vmsize / 权限 |
| 0（成功） | 失败在 exec 后续 | 可能要求 `LC_LOAD_DYLINKER`（真 dyld） |

### 今日最终状态

```
真机：UEFI → UniXNU → DarwinBoot → XNU → APFS 根 → 加载 init
内核：正确读到我们的 Mach-O 头（mockfs 通道，绕开 APFS 读取缺陷）
剩余：load_machfile 的段映射校验（EBADEXEC/LOAD_FAILURE）
```

---

## 3.45 cpusubtype 是 EBADEXEC 的元凶；修好后 exec 不再报错但卡在内部（2026-10-27 00:20）

### 决定性发现：`cpusubtype = 2` 是 arm64e

我们的 `make-minimal-init.py` 里把 `CPU_SUBTYPE_ARM64_ALL` 写成了 2，而：

```c
#define CPU_SUBTYPE_ARM64_ALL  ((cpu_subtype_t) 0)
#define CPU_SUBTYPE_ARM64_V8   ((cpu_subtype_t) 1)
#define CPU_SUBTYPE_ARM64E     ((cpu_subtype_t) 2)   /* ← 我们误用了这个 */
```

内核 `grade_binary()` 看到"arm64e 二进制 + 非 arm64e 内核" → 拒绝 → `exec_mach_imgact` 直接返回
**EBADEXEC(86)**（与观测完全一致）。

修正为 0 后重新生成 init（`init.macho` / `init64k.macho`），并作为 ramdisk 测试。

### 修正后的行为变化（重要）

| 阶段 | 修正前 | 修正后 |
|---|---|---|
| `/sbin/launchd` exec | 立即返回 EBADEXEC(86)，屏幕打印 `failed loading ... errno 8/86` | **不再打印 failed loading** |
| `exec_mach_imgact` | 立即返回 86 | **进入后不返回**（返回断点 0x76d078c 未命中） |

结论：cpusubtype 修正让 exec 通过架构 grade，进入真正的加载阶段，随后在内部卡住
（最可能是 `load_machfile` 的段页读取，经 mockfs 的 pagein/strategy 路径）。

### 明天下一步（按顺序）

1. `CATCH_EXEC=1` 断 `_load_machfile`(0xfffffff00775a8a8)：确认是否进入、是否返回
2. 若卡在 load_machfile 内部：
   * 检查 mockfs 的 `strategy/pagein` 是否正确转发到设备内存
   * 尝试 dense ramdisk（`--data-ramdisk`，设备内存直接映射）
3. 若 load_machfile 返回 0：看用户栈构建（copyout）与线程状态
4. 若一切成功：验证 `grep -c '0x00000001000001d8'`（inasm 里用户态入口）

### 今晚新增/更新的资产

```
scripts/make-minimal-init.py    cpusubtype 修正为 0（ARM64_ALL）
out/r11/init.macho              573 B 基线
out/r11/init64k.macho           64 KB 填充版（供 mockfs ramdisk 使用）
scripts/qemu-run-r11.sh         CATCH_EXEC 可在 exec_mach_imgact / load_machfile 之间切换
docs §3.43–§3.45                完整证据链
```

---

## 3.46 存储栈 Step1：NativeSdhci 编译通过 → 链接期撞上 IOStorageFamily，改走探测模式（2026-10-26 20:10）

### 结论先行

驱动**编译通过**（含 `-Werror` 与 ARM64/iOS 17 目标文件审计），但**内核链接失败**，
缺的 15 个符号全部来自 IOStorageFamily kext。这证明"能编译"和"能链接"是两件事，
也证明了我们的驱动写法在 ABI 层面是对的（类大小 `0x90`、190 槽 vtable、deployment 17.0 都对得上）。

```
archive-07: NativeSdhci: -Werror build and actual ARM64/iOS 17 object audit passed
ld: symbol(s) not found for architecture arm64
```

```
"vtable for IOBlockStorageDevice"
"IOBlockStorageDevice::gMetaClass"
"IOBlockStorageDevice::IOBlockStorageDevice(OSMetaClass const*)"
"IOBlockStorageDevice::~IOBlockStorageDevice()"
"IOBlockStorageDevice::init(OSDictionary*)"
"IOBlockStorageDevice::getProperty(OSSymbol const*) const"
"IOBlockStorageDevice::setProperties(OSObject*)"
"IOBlockStorageDevice::requestIdle()"
"IOBlockStorageDevice::doSynchronize(unsigned long long, unsigned long long, unsigned int)"
"IOBlockStorageDevice::doSynchronizeCache()"
"IOBlockStorageDevice::getWriteCacheState(bool*)"
"IOBlockStorageDevice::setWriteCacheState(bool)"
"IOBlockStorageDevice::doSetPriority(IOBlockStorageDeviceExtent*, unsigned int, unsigned char)"
"IOBlockStorageDevice::doGetProvisionStatus(unsigned long long, unsigned long long, unsigned int*, IOBlockStorageProvisionDeviceExtent*, unsigned long long)"
"IOBlockStorageDevice::doUnmap(IOBlockStorageDeviceExtent*, unsigned int, unsigned int)"
"IOStorage::complete(IOStorageCompletion*, int, unsigned long long)"
```

`NOTE: a missing vtable usually means the first non-inline virtual member function has no definition.`
—— 因为 `IOBlockStorageDevice` 的实现体不在 XNU 源码树里，它在 Apple 的 IOStorageFamily kext 中。

### 官方怎么解决的（机制，重要）

`sheng`（唯一官方 port）在 `device/board.toml:50-51` 里有两条归档：

```toml
"build-native-virtio-bindings.py --component NativeRamBlock --directory build/native-ram-block",
"build-native-virtio-bindings.py --component NativeVirtioBlock",
```

`UniXNU/scripts/build-native-virtio-bindings.py` 的尾部直接生成汇编再用 `ar` 重打包：

```python
assembly = out / 'original-bindings.S'
assembly.write_text(''.join(f'.globl {name}\n.set {name}, {value:#x}\n' for name, value in sorted(bindings.items())))
subprocess.run(['xcrun','clang','-target','arm64-apple-ios17.0','-c', assembly, '-o', out/'original-bindings.o'], check=True)
subprocess.run(['xcrun','ar','rcs', archive, obj_path, out/'original-bindings.o'], check=True)
```

即 **`.set 符号名, 绝对地址`**：把驱动里未定义的符号直接绑到"原版 IOStorageFamily 在
kernelcache 里被 slide 之后的真实地址"。链接期不需要符号体，运行期直接跳进原版 kext 代码。

地址来源是 `UniXNU/scripts/audit-storage-interface.py`：
1. 用公开的 Darwin 23 `refer/IOStorageFamily/IOBlockStorageDevice.cpp`（pin `e45fbe30a7879e0b16e26dddb78538f1b00e0bff`）编译出参考对象，得到 190 个虚函数槽的**顺序**，并校验 `DW_AT_byte_size == 0x90`、`methods.count('___cxa_pure_virtual') == 14`（`audit-storage-interface.py:93`）。
2. 去原始 kernelcache 里 `com.apple.iokit.IOStorageFamily` 的 `__DATA_CONST` 扫出这个 vtable（`audit-storage-interface.py:112-125`，要求 `matched == 176`），读到真实指针值。
3. 输入是 `build/native-prelink/native-apfs-load-positive.macho` + `.json`，且校验 `original_sha256 == f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7`。

**我们能用同一套**：`board.toml:89` 的 `kernelcache_sha256` 就是同一个值——我们和 sheng 用同一份
iPadOS 17.7.11（iPad7,1 / 21H461）IPSW，所以审计结果一致。

### 本次采用的折中：探测模式（B 方案）

`build-native-*-bindings.py` 需要一个我们目前没有的中间产物
（`build/native-prelink/native-apfs-load-positive.macho`），所以先绕开 storage 层：
把 `NativeSdhciBlock` 整段用 `#if XNU_NATIVE_SDHCI_BLOCK_DEVICE`（默认 0）关掉，
驱动退化成**纯 IOService 探测器**，链接期不再引入任何 storage 符号。

`drivers/NativeSdhci/NativeSdhci.cpp` 的改动：

| 位置 | 改动 |
|---|---|
| L32-41 | `XNU_NATIVE_SDHCI_BLOCK_DEVICE` 默认 0，`IOBlockStorageDevice.h`/`IOStorage.h` 变成条件 include |
| L95-97 | `class NativeSdhciBlock;` 前向声明条件编译 |
| L134-136 | `NativeSdhciBlock *fBlockDevice` 成员条件编译 |
| L139-174 / L784-967 | 块设备类的声明段与整段实现条件编译（含 `OSDefineMetaClassAndStructors`） |
| L465-495 | 新增 `readExtCsd()`：CMD8 读 512 B EXT_CSD，取 `SEC_COUNT[212..215]`（512 B 扇区数）、`EXT_CSD_REV[192]`、`BUS_WIDTH[183]`、`HS_TIMING[185]` |
| L497-590 | 新增 `probeMedia()`：读 LBA0（打印前 8 字节 + MBR 0x55AA 与 4 个分区项）、读 LBA1 校验 `"EFI PART"`、打印 GPT 头字段（entries/size/table_lba/first_usable/last_usable），再逐块扫分区表并打印前 16 个非空条目（名称从 72 B UTF-16LE 取 ASCII，类型 GUID 按前 3 段小端重排） |
| L704-759 | `start()` 的链路：`softReset` → `setBusPowerAndClock` → `cardInit` → `probeMedia`；块设备注册整段进 `#if` |
| L761-775 | `stop()` 的块设备清理进 `#if` |
| L179-210 | `probe()` 增加日志：匹配成功打 `matched DT node`；带 `sdhci` 字样但不匹配时打 `compatible=%.*s but no match`——避免真机上"静默失败"分不清哪一步 |

配套改动：
- `configs/r11_defconfig`：`CONFIG_XNU_NATIVE_RAM_BLOCK=n`（RamBlock 同样依赖 storage 符号；之前 `=y` 时内核脚本 `test -f libNativeRamBlock.a` 直接失败）。
- `board.toml`：`"build-arm-driver.py NativeRamBlock"` 注释掉，只留 `NativeSdhci`。
- `scripts/make-r11-adt.py:208-211`：`sdhci` 节点的 compatible 改成
  `qcom,sdhci-msm-v5\0qcom,sdm660-sdhci\0`（原来只有 `qcom,sdm660-sdhci`，与
  `NativeSdhci::probe()` 里匹配的 `qcom,sdhci-msm-v5` 不一致 → 真机上根本不会匹配），并加 `device_type = "sdhci"`。
  节点仍不带 `reg`：SDHCI 在 `0x0C0C4000`，落在 `arm-io` 的 `ranges` 窗口（`0x08000000`）之外，
  child reg 会溢出；物理窗口由驱动自己的 `R11_SDHCI_CORE_PHYS`/`R11_SDHCI_VENDOR_PHYS` 常量映射。

### 真机上应该看到的日志（验证清单）

```
NativeSdhci: matched DT node, probing controller
NativeSdhci: EXT_CSD rev=... SEC_COUNT=... (.... MiB) BUS_WIDTH=... HS_TIMING=...
NativeSdhci: LBA0 ok sig=0000000000000000
NativeSdhci:   MBR p1 type=0xee start=1 count=...
NativeSdhci: GPT ok entries=128 size=128 table_lba=2 usable=... 
NativeSdhci:   GPT[1] pmic ... type=...
NativeSdhci:   GPT[n] ... (R11 有 69 个分区)
NativeSdhci: probe mode, N blocks, block device NOT registered
```

- `SEC_COUNT == 0` 或 `EXT_CSD` 读失败 → CMD8 之前的时钟/总线宽度没设对（`setBusPowerAndClock()` 目前固定 400 kHz、8-bit、1.8 V）。
- `LBA0 ok` 但全是 0 → 命令通了但数据相位没取到（PIO 读的 `SDHCI_DATA_AVAILABLE` 轮询或 `TRANSFER_MODE`）。
- `no GPT` 但 MBR p1 type=0xee → GPT 头不在 LBA1（少见，多为 4K 逻辑扇区）。
- 完全没有 `NativeSdhci:` 开头任何一行 → ADT 里没有 `sdhci` 节点或 compatible 没改对（用 `probe saw compatible=...` 那行区分）。

### 风险与已知限制

- **QEMU 没有 sdhci-msm**，这一步只能在真机上验证；CI 只能保证编译与链接。
- `cardInit()` 仍走默认速度（未做 CMD6 切 HS200），够读分区表，读大文件会慢。
- 写路径仍未实现（`writeBlocks` 返回 `kIOReturnNotWritable`），探测阶段不写任何东西。
- 探测发生在 `NativeSdhci::start()`，依赖 IOKit 把 ADT 节点当 provider 发出来——`ArmDTPlatform` 的匹配路径此前只被 `ArmCpu`/`NativeAES` 用过，这是第一次用它匹配外设节点。

### 下一步（Step 2 → Step 3）

1. 真机跑一次，按上面的清单确认卡初始化与 GPT。
2. 若链路通：补 `build-native-sdhci-bindings.py`（照 `build-native-virtio-bindings.py` 写，`--component NativeSdhci`），
   并生成它需要的 `build/storage-abi/interface-audit.json`（先解决 `native-apfs-load-positive.macho` 的生产者），
   打开 `-DXNU_NATIVE_SDHCI_BLOCK_DEVICE=1` 把块设备层接回去。
3. 然后才是 `IOBlockStorageDevice` → `IOMedia` → APFS 挂 System 卷 → 真 `launchd`。

---

## 3.47 内核链接 + prelink 通过；payload 卡在种子文件名（2026-10-26 20:30）

### 结论先行

去掉 `IOBlockStorageDevice` 继承之后，**整条内核链一枪过**：编译、链接、prelink、打包 UniXNU 全部成功。

```
== kernel: bootstrap pass for the compile records      ← 第一遍故意失败，只为出编译记录
Undefined symbols: _mockfs_mountroot / _native_legacy_display_probe
== kernel: prepare storage headers → PASS: 19 public IOStorageFamily headers prepared
== kernel: archive 7/10: build-arm-driver.py NativeSdhci
NativeSdhci: -Werror build and actual ARM64/iOS 17 object audit passed
== kernel: LDFILELIST bsd / LD mach.development.bcm2837.unstripped.noctf   ← 无 undefined
== kernel: prelink → Experimental candidate: 53997568 bytes
== kernel: kernel.macho sha256 bc1f9118dd3d54d1c74bc9dcf1f1fa53b01d49de17eba819713f040f145fec91
== package: UniXNU → UniXNU.efi 93696 / UniXNUDxe.efi 14336
```

`kernel.macho`（53,997,568 B）已经能从 Artifacts 下载到 —— 这一轮**内核侧没有剩活**。

### payload 没出来的根因：种子文件名

`make-data-seed.sh` 的产物是 `<prefix>.raw` / `<prefix>.dbr` / `<prefix>.uuid`，
**`<prefix>` 本身不是文件**。workflow 里却写的是：

```yaml
bash scripts/make-data-seed.sh "$RUNNER_TEMP/seed.dbr" 4096     # 前缀叫 seed.dbr
./unixnu build -D ../device payload --data-ramdisk "$RUNNER_TEMP/seed.dbr"   # 这个路径不存在
```

于是 payload 步骤实际收到的是一条没有 `--data-ramdisk` 的 `--skip-setup` 命令，被工具直接拒掉：

```
unixnu: --skip-setup edits the Data seed: pass the original one with --data-ramdisk FILE
```

也就是说：**种子本身是成功的**（`F192F7EA-089E-4800-B30C-18F310A407EB`，APFS，
三个 launchd 路径 65536 B 0755 都写进去了），坏的只是传参。

### 本轮改动

| 位置 | 改动 | 原因 |
|---|---|---|
| workflow 种子步骤 | `continue-on-error: true`、`ls seed.dbr.*` | 种子失败不许再拦内核产物；`ls` 不能再指向不存在的文件（`bash -e` 会红） |
| workflow 种子大小 | 4096 → 32 MB | 128 MiB 的 payload 要装 51.5 MiB 内核，4096 MB 根本塞不进；`hdiutil` 也快得多 |
| workflow UUID 步骤 | 没有 `.uuid` 就 `exit 0` | 幂等 |
| workflow payload 步骤 | 传 `$RUNNER_TEMP/seed.dbr.raw`；没有就退回不带 `--skip-setup` | 稠密 raw 才是 `/chosen/memory-map/RAMDisk` 要的东西 |
| workflow 产物上传 | `if: always()` | 默认 `success()` 会让"种子红 → 产物全丢"重演 |
| workflow 日志上传 | 增加 `/tmp/seed.log` | 上次种子为什么红完全看不到 |
| `board.toml` | `packed_data_ramdisk = true` → `false` | `true` 会写成 `PackedRAMDisk`（DBRDISK1），那条路要 `NativeRamBlock`；XNU 只认 `RAMDisk` 键名，设 true 等于根卷静默挂不上 |

顺带修掉一个 bash 3.2 的坑：macOS runner 的 `/bin/bash` 是 3.2，`set -u` 下展开空数组
（`"${EXTRA[@]}"`）会直接 `unbound variable`，已改成普通字符串拼接。

### 状态

- ✅ 内核：编译 / 链接 / prelink / 打包全绿，产物可下载。
- ⏳ payload：等下一轮 CI（改动已就位，只差跑一次）。
- ⏳ 真机探测：拿到 `payload.img` 后打进 `boot.img` 刷机，观察
  `NativeSdhci: matched DT node` → `EXT_CSD ... SEC_COUNT` → `GPT ok ... GPT[1]`。


## 3.48 真机第二次启动：内核跑起来了，但 NativeSdhci 一声不吭（2026-10-26 21:10）

### 现象

打过补丁的 payload 刷进去后，屏幕不再停死在菜单帧，内核日志整篇滚出来：

- `Darwin Image4 Validator Version 6.3.0`、`Security policy loaded: AppleImage4 hooks`
- `AppleImage4: failed to read nonce slot data: 2`（七个 magazine 各一条，没有 nonce 盘是预期）
- `calling mpo_policy_init for AMFI` / `Seatbelt sandbox policy (Sandbox)` / `no external trust caches found`
- `apfs_module_start:3232: load: com.apple.filesystems.apfs, v2236.140.6`
- `dill_init: all the create dill kernel threads have been scheduled`
- `Skylwalk: sk_copy64_8 ... PASSED` 整套 17 项
- `bsd_autoconf: calling kinit`、`ifnet_attach: ... lo0`（这一轮才第一次到 kinit）
- `Install_apfs_vfs_fs:8248`、`IOSurfaceRoot::installMemoryRegions()`
- 最后一行：`Waiting on <dict ID="0"><key>IOProviderClass</key><string ID="1">IOMedia</string>...<string ID="3">2E551AA5-5686-458D-B878-B467DF68CDB2</string></dict></dict>`

也就是内核在等 UUID `2E551AA5-...` 的 IOMedia。payload 这轮**不带 Data ramdisk**
（`payload.json` 的 `data_ramdisk_sha256` 是 null），eMMC 又没有块设备，所以永远等下去。

**而且整篇日志里没有一条 `NativeSdhci:`。**

### 根因：驱动类没有 personality，内核根本不会去匹配它

UniXNU 的 XNU 驱动是静态链进内核的 C++ IOKit 类，**内核只从 `iokit/KernelConfigTables.cpp`
的 `gIOKernelConfigTables` 里认 personality**；`Info.plist` 只是给人看的。

交叉验证：

| 检查 | 结果 |
|---|---|
| `kernel.macho` 里 `<string>NativeSdhci` / `<string>ArmDTPlatform` / `<string>ArmCpu` | 0 次 |
| `kernel.macho` 里 `IOKitPersonalities` | 25 次（全是 Apple 自己的） |
| `out/r11/tree/.cache/xnu-work/iokit/KernelConfigTables.cpp` | 有 `ArmCpu`/`ArmVirtPlatform`/`ArmDTPlatform`/`IOPanicPlatform`/`IOExclaveProxy` + `XNU_NATIVE_AES`/`VIRTIO_BLOCK`/`VIRTIO_CONSOLE`/`RAM_BLOCK`/`UFS` 五段，**没有 `XNU_NATIVE_SDHCI`** |

`patch-tree-for-sdhci.py` 之前只做了两件事：给编译加 `-DXNU_NATIVE_SDHCI=1`、把
`libNativeSdhci.a` 用 `-Wl,-force_load` 链进去。类在内存里，但没有任何 personality 指向
`IOClass = NativeSdhci`，`probe()` 一次都不会被调用 —— 所以日志干净得可疑。

### 修法：构建期把 personality 补丁投递进 `patches/xnu/`

`build-xnu-4k-baseline.sh:44` 是 glob 应用补丁：

```sh
for PATCH in "$ROOT"/patches/xnu/*.patch "$ROOT"/kits/NativePrelink/patches/*.patch; do
```

所以只要 `$ROOT/patches/xnu/` 里多一个文件就会被应用。`patches/xnu/native-aes-personality.patch`
就是这个格式（`@@ -61,4 +61,48 @@`，追加在 `")";` 之前）。

**不能**把 .patch 直接放设备仓库的 `patches/` 下：`tree.py:44,69` 把 patches 当**模块整目录
链接**，设备仓库一旦出现 `patches/xnu`，同名的 `xnu` 模块会盖掉主仓库那套上游补丁，
上游几十个 xnu 补丁会全部消失。所以改成：

| 文件 | 作用 |
|---|---|
| `unixnu-r11/scripts/data/native-sdhci-personality.patch` | 唯一副本（454 B） |
| `unixnu-r11/scripts/make-sdhci-personality-patch.py` | 从装配后的 `KernelConfigTables.cpp` 生成它（往 `")";` 前插块，写 LF；`write_text` 在 Windows 上会写成 CRLF，patch 就永远 apply 不上） |
| `unixnu-r11/scripts/patch-tree-for-sdhci.py` | CI 里把补丁投递到 `$ROOT/patches/xnu/`（已存在的树也覆盖一份） |

补丁内容：

```
@@ -105,4 +105,11 @@
     "     'IOProbeScore'    = 500:32;"
     "   }"
 #endif
+#if defined(XNU_NATIVE_SDHCI) && XNU_NATIVE_SDHCI
+    "   ,{"
+    "     'IOClass'         = NativeSdhci;"
+    "     'IOProviderClass' = IOPlatformDevice;"
+    "     'IOProbeScore'    = 1000:32;"
+    "   }"
+#endif
     ")";
```

provider 选 `IOPlatformDevice`：ADT 里 `sdhci` 节点是 `arm-io` 的子节点
（`make-r11-adt.py`，`compatible = qcom,sdhci-msm-v5\0qcom,sdm660-sdhci\0`，无 `reg`），
`ArmDTPlatform::createDeviceNubs()` 给每个子节点建 nub，UFS 用同一套。

本地验证（WSL，`/root/src/UniXNU`）：脚本两次运行幂等；补丁投递后
`git -C .cache/xnu-work apply --check` 通过；apply 后 `KernelConfigTables.cpp:110` 出现
`"     'IOClass'         = NativeSdhci;"`，回滚干净。

### 下一步

1. 重跑 CI → 下载 `kernel.macho` + `payload.img`。
2. `python3 scripts/patch-payload-full.py out/r11/payload/payload.img` 打补丁（**CI 产物从不自动打**）。
3. `build-stage1.sh` 出 `boot.img`（7,864,320 B）→ `dd` 刷入 → 看 `NativeSdhci: matched DT node`。
4. 若 personality 生效但 `Waiting on IOMedia` 仍在最前面：给 payload 加回一个最小 Data ramdisk，
   让根卷等待有解，内核才会继续走到 exec。

## 3.49 NativeSdhci v4：根时钟 CMD_ROOT_EN=0 才是"读寄存器整机挂死"的根因（2026-10-27）

v3 的真机日志停在 `start: reading version/caps`，一行 panic 都没有 —— 说明不是 aborts，
而是 AXI 事务没有响应、CPU 静默卡住。v3 顺带打出的 GCC 状态直接指认了原因：

```
NativeSdhci[v3]: gcc: apps=0x00004221 ahb=0x20008001 ice=0x00004220
NativeSdhci[v3]: gcc: rcg cfg=0x00000000 m/n/d=0x0000050f/0x00000001/0x000000fe
```

* 分支门控（`gcc_sdcc1_apps_clk` 0x16004、`gcc_sdcc1_ahb_clk` 0x16008）的 bit31
  （`CBCR_CLK_OFF`）都是 0，看着"已开"；
* 但 `0x1602c` 读出来是 0，而按 `drivers/clk/qcom/clk-rcg2.c` 的布局，
  `cmd_rcgr + 0x00` 是 **CMD_RCGR**（bit0=CMD_UPDATE、bit1=CMD_ROOT_EN、bit31=CMD_ROOT_OFF），
  `+0x04` 才是 CFG。所以真值是 **CMD_ROOT_EN=0** —— `sdcc1_apps_clk_src` 的根时钟没有输出。
* 分支放行但输入无时钟 ⇒ SDCC 整个模块没有时钟 ⇒ 读 SDCC 寄存器时总线不响应，整机静默挂死。
  （顺带纠正：v3 日志把 CFG/M/N/D 读错位了一格，现在按 clk-rcg2 的偏移重读。）

### v4 的改动

1. 新增 `enableGccClocks()`，完全按 `clk_rcg2` 的顺序自开根时钟：
   `CMD=0`（关根）→ `CFG=0`（SRC_SEL=XO、SRC_DIV=1、MODE=0）→ `M/N/D=0` →
   `CMD=CMD_UPDATE` → `CMD=CMD_ROOT_EN` → 轮询 `CMD_ROOT_OFF` 清零；
   再对 ahb/apps 两个 CBCR 读改写置 bit0，并轮询 `CLK_OFF` 清零。任一步失败即放弃，
   **绝不在没有时钟的情况下读 SDCC**。
2. 父时钟固定用 **XO 19.2 MHz**（板上晶振，永远在跑），不赌 GPLL0/GPLL4 是否已配置 ——
   bring-up 阶段要自给自足。基础时钟因此是 19.2 MHz。
3. 新增 `setSdclkDivider(div)`：`SDCLK = 19.2 MHz / (2 * div)`。
   识别用 `div=24`（400 kHz），`cardInit()` 成功后升到 `div=1`（9.6 MHz）再读 EXT_CSD/GPT。
   （v3 之前写死 `div=62` 且假定 base 50 MHz，是错的。）
4. 版本标记升到 `v4`，屏幕上看 `NativeSdhci[v4]:` 即确认刷进去了。

### 判定表（v4）

| 日志 | 结论 |
|---|---|
| `gcc clocks not ready` | 根时钟打不开（电源域/地址/父时钟问题），要查 GCC 或 SDCC 电源域 |
| `root enabled -> cmd=…` 后没了 | 分支仍无时钟，或 SDCC 本身还需解复位 |
| `branch ahb=… (CLK_OFF stuck)` | 该分支被别的子系统占用/硬件门控 |
| `ver=… caps=…` 出现 | **总线终于通了**，进入 SDHCI 协议层 |
| `internal clock never stabilised` | SDCLK 没起来（分频/供电） |
| `cardInit: OCR=…` | 卡识别成功，后面看 EXT_CSD/GPT |

## 3.50 NativeSdhci v5：寄存器位宽错了 —— 0xFE 的错位 32 位读就是"整机静默"的根因（2026-10-27）

v4 已经把 GCC 的 `sdcc1_apps_clk_src` 根时钟（`CMD_ROOT_EN`）和 ahb/apps 两个分支
全部打开，实测日志也确认 `cmd=00000002`（`ROOT_OFF` 已清）、分支 `CLK_OFF=0`，
但**第一条 SDCC 寄存器访问依然把整机挂死**（`start: reading version/caps` 之后无声无息）。
这否定了"时钟没开"这条假设，把矛头指向访问方式本身。

### 3.50.1 根因：全程用 32 位访问 8/16 位寄存器

`SdhciRegs.h` 里的偏移都是**字节偏移**，但 SDHCI 规范里这些寄存器位宽不同：

| 位宽 | 偏移 |
| --- | --- |
| 32 位 | `0x00` ARGUMENT、`0x10..0x1C` RESPONSE、`0x20` BUFFER、`0x24` PRESENT_STATE、`0x40` CAPABILITIES、`0x44` CAPABILITIES_1、`0x48` MAX_CURRENT、`0x58` ADMA_ADDRESS、`0xFC` SLOT_INT_STATUS |
| 16 位 | `0x04` BLOCK_SIZE、`0x06` BLOCK_COUNT、`0x0C` TRANSFER_MODE、`0x0E` COMMAND、`0x2C` CLOCK_CONTROL、`0x30..0x36` 中断状态/使能、`0x3C` AUTO_CMD_STATUS、`0x3E` HOST_CONTROL2、`0xFE` HOST_VERSION |
| 8 位 | `0x28` HOST_CONTROL、`0x29` POWER_CONTROL、`0x2A` BLOCK_GAP、`0x2B` WAKE_UP、`0x2E` TIMEOUT_CONTROL、`0x2F` SOFTWARE_RESET |

v3/v4 的 `sdhciRead32`/`sdhciWrite32` 对全部偏移一视同仁，于是：

1. **第一枪就打偏**：`sdhciRead32 (fCore, SDHCI_HOST_VERSION)` 是对 `0xFE` 的 32 位读 ——
   `0xFE % 4 == 2`，这是一次**错位**访问。Snapdragon 的 SDCC AHB 从口不做错位访问的
   拆分，事务永远不完成，CPU 停在那条 `ldr` 上，连 data abort 都不产生 → **整机静默**。
   这就解释了为什么 `MACH Reboot`/panic 一条都没有。
2. 即使读通了，后面每一步也是错的：写 `0x04` 会连带写掉 `BLOCK_COUNT`、写 `0x0C` 会
   连带写掉 `COMMAND`、写 `0x2C` 会连带写掉 `TIMEOUT_CONTROL`/`SOFTWARE_RESET`、
   写 `0x28` 会连带写掉 `POWER_CONTROL`/`BLOCK_GAP`/`WAKE_UP`。

### 3.50.2 v5 的改动（`drivers/NativeSdhci/`）

- `NativeSdhci.cpp` 新增 `sdhciRead16`/`sdhciRead8`/`sdhciWrite16`/`sdhciWrite8`；
  写后回读改成**同一偏移、同一宽度**（`BUFFER` 例外，FIFO 回读会吃掉数据字）。
- `sdhciWaitSet16`：`CLOCK_CONTROL` 是 16 位寄存器，不能拿 32 位轮询。
- 逐点修正：`softReset`（0x2F，8 位）、`setBusPowerAndClock`（0x28/0x3E/0x2E/0x29）、
  `setSdclkDivider`（0x2C）、`waitCommandComplete`/`waitTransferComplete`（0x30/0x32）、
  `sendCommand`（0x0E，以及把 `0xFFFFFFFF` 改成 `0xFFFF`）、`readExtCsd`/`readBlocks`/
  `writeBlocks`（0x04/0x06/0x0C）、`start` 的 version 读（0xFE，16 位）。
- `start()` 的探活顺序改成"先对齐、后错位"：先打 `start: probing SDCC @0x24 (32-bit, aligned)`
  读 `PRESENT_STATE`，再打 `start: probing SDCC @0xFE (16-bit)` 读 `HOST_VERSION`，
  这样万一下一版还挂，从最后一行日志就能判断是"整块无响应"还是"只是 0xFE 那条不行"。
- **总线宽度改回 1-bit**：eMMC 上电默认 1-bit，DAT1..DAT7 要等 CMD6 的
  `EXT_CSD[183] BUS_WIDTH=2` 才可用。v4 在识别前就把 host 设成 8-bit，即使寄存器读通，
  CMD8(SEND_EXT_CSD) 的数据也会因为卡只驱动 DAT0 而 CRC 错。
- **`HOST_CONTROL2` 的 1.8V Signaling Enable 不再置位**：那是 SD 卡 UHS 用的位，eMMC 不用。
- 新增 `POWER_CONTROL` 写 `SDHCI_POWER_ON | SDHCI_POWER_330`（`SdhciRegs.h` 里补了
  `SDHCI_POWER_ON`/`SDHCI_POWER_330`/`SDHCI_POWER_300`/`SDHCI_POWER_180`），
  并回读打印，确认控制器开始驱动时钟。
- `#define kSdhciTag "v5"`，所有日志前缀跟着变成 `NativeSdhci[v5]:`。

### 3.50.3 下一版判读表

| 屏幕上最后一行 | 结论 | 下一步 |
| --- | --- | --- |
| `start: probing SDCC @0x24 (32-bit, aligned)` | 整块无响应（不是位宽问题） | 查 SDCC 复位/电源域，或从 UEFI 阶段先摸一次寄存器 |
| `state=…` 但停在 `probing SDCC @0xFE` | 0xFE 那条还是不行 | 拆成两次 16 位半字读 / 改从 `fVendor` 窗口读 |
| `ver=… caps=…` | 控制器活了 | 看 `reset done`/`clock: … stable` |
| `bus: hostctl=… power=…` | 总线配置通了 | 看 `cardInit: CMD0/CMD1(OCR=…)` |
| `cardInit: OCR=…` / `CMD3 (rca=1)` | 卡识别成功 | 看 `EXT_CSD … SEC_COUNT` |
| `GPT ok …` / `GPT[1] …` | **链路完全打通** | 进 Step 4：块设备注册 + IOMedia |

## 3.51 NativeSdhci v6：8 位访问也会挂死总线 —— 全部改 32 位读-改-写（2026-10-27）

### 3.51.1 真机 v5 结果（决定性一步）

v5（寄存器位宽修正）刷机后，屏幕日志第一次读出了控制器身份：

```
NativeSdhci[v5]: start: probing SDCC @0x24 (32-bit, aligned)
NativeSdhci[v5]: state=01f800f0
NativeSdhci[v5]: start: probing SDCC @0xFE (16-bit)
NativeSdhci[v5]: ver=02 caps=742dc8b2 caps1=0000000f
```

- `ver=02` → SD Host Controller Spec 3.0，控制器活着；
- `state=01f800f0` → `CARD_INSERTED` + `CMD/DAT line` 状态都在（eMMC 焊在板上）；
- `caps` 里的 base clock / 8-bit 支持位都在；
- 关键对照：**32 位对齐读（0x24）与 16 位读（0xFE）都通**，v3/v4 的"整机静默"确实就是 `0xFE` 的错位 32 位读。

但 `ver=` 之后**再无一条 `NativeSdhci` 日志**，而启动线程没有停：后面照样出现
`dlil_init: All the created dill kernel threads…`、`Skylwalk: …`、`bsd_autoconf: calling kminit`，
最终停在 `Waiting on <dict ... IOMedia ... {本轮 seed UUID}>`。

也就是说 `NativeSdhci::start()` 在 `ver=` 之后卡死（不是 panic，不是失败返回 ——
`fail:` 只 `return false` 不打日志，但那样内核会继续往下走而我们看到了 IOMedia 等待，
说明 start 是在某个访问上永久停住）。

### 3.51.2 定位：`ver=` 之后的第一件事是 8 位写

`ver=` 到 `start: reset done` 之间只有一次 `softReset(SDHCI_RESET_ALL, kSdhciCmdLoops)`：

```c
sdhciWrite8 (fCore, SDHCI_SOFTWARE_RESET, which);   /* 0x2F，8 位 */
for (i < 200000) {
    if ((sdhciRead8 (fCore, SDHCI_SOFTWARE_RESET) & which) == 0) return true;
    IODelay (10);
}
```

`kSdhciCmdLoops = 200000`，每轮 `IODelay(10)` → 2 s 后必然打 `reset timed out`。
日志里没有这句，所以卡在**写 0x2F 或第一次读 0x2F**。

结论：**SDCC 的 AHB 从口对 8 位访问同样不应答**（32 位对齐、16 位都通，唯 8 位不通）。
原因在 SoC 侧：SDCC 的 register block 是按 32 位字组织的，只有特定偏移（0xFE、0x0E、0x2C…）
在硬件里真的接出了 16 位通道；纯 8 位事务没有对应的 AHB 子节拍，事务不完成 → CPU 停在 `ldr/strb`。

### 3.51.3 v6 做法：不再有任何 8 位访问

8 位寄存器全部落在两个 4 字节对齐的字里，改用整字读-改-写：

| 字地址 | 字节 0 | 字节 1 | 字节 2 | 字节 3 |
|---|---|---|---|---|
| `0x28` | `HOST_CONTROL` (8b) | `POWER_CONTROL` (8b) | `BLOCK_GAP` (8b) | `WAKE_UP` (8b) |
| `0x2C` | `CLOCK_CONTROL` (16b, 占 0x2C..0x2D) | `TIMEOUT_CONTROL` (8b, 0x2E) | `SOFTWARE_RESET` (8b, 0x2F) | — |

改动清单（`drivers/NativeSdhci/NativeSdhci.cpp`，`kSdhciTag` = `v6`）：

1. **删除** `sdhciRead8` / `sdhciWrite8`（函数整体移除，防止以后又被用上）。
2. 新增 `sdhciReadHostWord()` / `sdhciReadClockWord()` / `sdhciWriteClockWord()`。
3. `softReset()`：读 0x2C 字 → 把 `which` 写进 bits[31:24] → 轮询 `(word >> 24) & which == 0`；
   日志 `reset: assert=%02x word %08x -> %08x` / `reset: cleared after N polls` / `reset: never cleared`。
4. `setBusPowerAndClock()`：`HOST_CONTROL` + `POWER_CONTROL` 一次 32 位写（`word = (old & 0xFFFF0000) | (POWER<<8) | hostCtrl`）；
   `TIMEOUT_CONTROL` 走 0x2C 字的 bits[23:16]；`HOST_CONTROL2` 仍是 16 位（0x3E，已验证通路）。
5. `setSdclkDivider()`：只改 0x2C 字低 16 位，`sdhciWaitClock()` 轮询 `CLOCK_CONTROL` 稳定位。
6. `start()` 在 `ver=` 之后插入 **IODelay 隔离探针**：`delay probe: begin` → `IODelay(100)` → `delay probe: done`。
   之前的 `IODelay` 大概率一次都没真正执行过（轮询首轮就命中），所以必须单独验证这个 API。

### 3.51.4 下一刷的判读表

| 屏幕最后一行 | 结论 | 下一步 |
|---|---|---|
| `delay probe: begin` 之后断 | `IODelay` 在本平台不可用 | 换成自旋等待（读 CNTPCT_EL0） |
| `reset: assert=01 word ...` 之后断 | 32 位写 0x2C 也不通 | 查 SDCC 复位源/电源域，而不是总线宽度 |
| `reset: never cleared` | 复位位不自动清零 | 软复位被 SoC 侧保持，需要 CRG/复位控制器 |
| `bus: hostctl2=...` | 复位通过 | 看 `clock: div=24` 是否稳定 |
| `cardInit: OCR=...` | CMD 线通 | 等 CMD2/CMD3/CMD9 全过 → `EXT_CSD ... SEC_COUNT` |
| `GPT ok ... GPT[1] ...` | 全链路通 | 进入 Step 4：块设备注册（15 个 IOStorageFamily 符号的 `.set` 绑定） |

## 3.52 NativeSdhci v7：`IODelay` 在本平台不返回 —— 改用 CNTPCT_EL0 自旋（2026-10-27）

### 3.52.1 v6 真机结果：探针自证

v6 只做了一件事就能定性：在 `ver=` 之后插一对 `delay probe` 日志。真机输出：

```
NativeSdhci[v6]: ver=02 caps=742dc8b2 caps1=0000000f
NativeSdhci[v6]: delay probe: begin
        ← 没有 delay probe: done
dill_init: All the created dill kernel threads have been scheduled at least once
Skylwalk: running self-tests ...
bsd_autoconf: calling kminit
...
Waiting on <dict ... IOMedia ...>
```

结论（三条同时成立）：

1. `delay probe: begin` 打出来了 → `ver=` 之后的 8 位寄存器访问**已经被 v6 的整字读写改掉**，不再是卡点；
2. `delay probe: done` 没出现 → **`IODelay(100)` 不返回**；
3. 内核其它线程继续跑、启动流程照常 → 停下的是 IOKit 匹配线程，不是整机。

`v6` 之前所有 `IODelay` 调用都是"轮询首轮就命中"的路径，从来没真正执行过，所以这个缺陷一直没暴露。

### 3.52.2 为什么 `IODelay` 会不返回

`IODelay` 依赖内核已接管的时间基准（ARM generic timer 注册成功）。我们这套 ADT 里
`pe_arm` 连中断控制器都没找到：

```
pe_arm_map_interrupt_controller: soc_phys: 0x17000000
pe_arm_map_interrupt_controller: failed to find the interrupt-controller.
pe_arm_init_debug: failed to find cpu-debug-interface
```

时间基准没有起来时，任何基于它的睡眠都会永久自旋。`IOSleep` 同理（它还多一层调度器依赖）。

### 3.52.3 v7 做法

自建延迟，不碰内核定时器：

```c
static inline uint64_t sdhciCounter (void)     { mrs x, cntpct_el0 }   /* 物理计数器 */
static inline uint64_t sdhciCounterFreq (void) { mrs x, cntfrq_el0 }   /* 计数频率 */
static void sdhciDelayUs (uint32_t us);
```

- 正常路径：`ticks = (freq / 1e6) * us + 1`，自旋 `yield` 直到 `cntpct` 差值达标；
- 兜底路径：`freq` 为 0 或不合理时，退化成 `us * 64` 次 `yield` 自旋；
- 再加一层 `guard`（40M 次）防止计数器真的不走时死循环。

替换范围：`IODelay` 12 处、`IOSleep(10)` 2 处（`cardInit` 的 CMD1 重试）全部换成 `sdhciDelayUs`。
`kSdhciTag` = `v7`；`delay probe` 日志升级为打印 `cntfrq` 与推进的 tick 数：

```
delay probe: begin (cntfrq=19200000 cntpct=...)
delay probe: done (advanced=1920 ticks)      /* 100 us @ 19.2 MHz */
```

### 3.52.4 判读表（v7）

| 最后一行 | 结论 | 下一步 |
|---|---|---|
| `delay probe: done (advanced=…)` | 延迟好了 | 继续看 `reset:` 系列日志 |
| `delay probe: done (advanced=0)` | 计数器不走 | ADT 缺 timer 节点，要补 CNTFRQ/中断 |
| `delay probe: begin` 后断 | 连 `mrs cntpct_el0` 都trap | 查 HCR_EL2/串口级 EL 状态（几乎不可能） |
| `reset: cleared after N polls` | 软复位通过 | 看 `bus:` 与 `clock:` |
| `reset: never cleared` | 32 位写 0x2C 也不生效 | SDCC 复位被 SoC 侧保持，要看 CRG/复位控制器 |
| `cardInit: OCR=...` | CMD 线通了 | 等 `CMD2/CMD3/CMD9` → `EXT_CSD ... SEC_COUNT` |

### 3.52.5 v7 首轮 CI 编译错误（已修）

`build-arm-driver.py` 用内核自己的编译配方，带 `-Werror -Wdeprecated-volatile` 和严格 C++ 语义，
v7 第一版撞了两处：

1. `-Wdeprecated-volatile`：`for (volatile uint32_t i = 0; ...; i++)` —— C++20 起对 volatile
   自增已弃用。改成**非 volatile** 循环变量 + 循环体内 `__asm__ volatile ("yield")`
   （asm volatile 保证循环不被优化掉）。
2. `cannot jump from this goto statement to its label`：`start()` 里探针的
   `uint64_t probeFreq / probeTicks` 声明在若干 `goto fail` 之后，C++ 禁止 goto 跳过带初始化
   的局部变量。把探针整体套进一个 `{ ... }` 作用域即可（标签在作用域之外，不构成"跳进作用域"）。

教训：这个驱动文件里 `goto fail` 很多，**新增局部变量一律放在函数开头或独立作用域里**。

## 3.53 NativeSdhci v7 真机结果：延迟修好了，`RESET_ALL` 卡住；v8 改为 CMD|DATA 复位（2026-10-27）

### 3.53.1 真机日志（v7）

```
NativeSdhci[v7]: ver=02 caps=742dc8b2 caps1=0000000f
NativeSdhci[v7]: delay probe: begin (cntfrq=19200000 cntpct=18446744085119878400)
NativeSdhci[v7]: delay probe: done (advanced=68590475116 ticks)
NativeSdhci[v7]: start: asserting software reset (0x2C word)
NativeSdhci[v7]: reset: assert=01 word 000f0007 -> 01000003
NativeSdhci[v7]: reset: never cleared, word=01000003
NativeSdhci[v7]: reset timed out
```

三条结论：

1. **`IODelay` 确认是本平台的坑**：换成 `CNTPCT_EL0` 自旋后 `delay probe: done` 打出来了，
   `cntfrq=19200000`（19.2 MHz，与 GCC 侧 XO 一致）。§3.52 的诊断成立。
2. **`IOLog` 的 `%llu` 不可信**：打印出的 `cntpct=18446744085119878400` 已经大于 `2^64`
   （18446744073709551616），`advanced=68590475116` 也对不上 100 us（应为 1901 tick）。
   所以 v8 起所有 64 位值改成 `%08x%08x` 两段打印。
3. **`RESET_ALL` 在这个平台上不会清零**：写之前 `CLOCK_CONTROL` 字是 `000f0007`
   （低 16 位 `0x0007` = INT_CLK_EN + INT_CLK_STABLE + SD_CLK_EN，说明 **ABL 交接时控制器
   已经被初始化好了、卡时钟已经在跑**；`0x0f` 是 TIMEOUT_CONTROL 默认值），
   写 `01` 后读回 `01000003`（复位位一直挂着，只有 SD_CLK_EN 被复位清掉），200 ms 后仍是 `01000003`。

### 3.53.2 推论：上一轮"8 位访问挂死总线"的判断要修正

v5 在 `ver=` 之后静默，当时归因为 `sdhciWrite8(0x2F)`。现在看更可能是这样：
8 位写没问题，**是紧接着的轮询里第一次 `IODelay(10)` 挂住了**（v5 的 `softReset` 只有在
复位位立刻清零时才不进 `IODelay` 分支，而 `RESET_ALL` 不会立刻清零）。
v6 换成整字读写之后仍然停在 `delay probe`，正好把这两件事分开了。
整字访问保留（无害且少一次对齐风险），但结论按 v7 的证据改写。

### 3.53.3 v8 改动

1. `softReset()` 不再是致命错误：超时后**强制把复位字节写 0**，打
   `reset: never cleared (word=…), forced 0 -> …, continuing`，然后继续往下做。
2. `start()` 改用 `SDHCI_RESET_CMD | SDHCI_RESET_DATA`（`0x06`）：只复位命令/数据状态机，
   不碰时钟与电源 —— 全复位在我们这种"bootloader 已初始化"的环境里只会把卡时钟关掉。
3. 所有 `CLOCK_CONTROL` 写入都只保留 `TIMEOUT_CONTROL` 字节，**复位字节恒写 0**
   （`tail = word & 0x00FF0000`），避免残留的复位位挡住后续时钟使能。
4. 探针日志改成 32 位两段（`cntpct=%08x%08x`、`advanced=%08x%08x`）。

### 3.53.4 判读表（v8）

| 屏幕出现 | 结论 | 下一步 |
|---|---|---|
| `delay probe: done (advanced=…)` | 计数器正常（100 us ≈ `000000000000076d` @ 19.2 MHz） | — |
| `reset: never cleared …, continuing` | 复位位卡住但已强制清掉，属**预期** | 看下一行 `bus:` |
| `bus: hostctl2=…` | 电源/位宽写进去了 | 看 `clock: div=24 … stable` |
| `internal clock never stabilised` | GCC 基频或分频写没生效 | 复核 `sdcc1_apps_clk_src` 分频（M/N/D） |
| `clock: div=24 (400 kHz) … stable` | SDCLK 起来了 | 等 `cardInit: CMD0` |
| `cardInit: OCR=…` | CMD 线通、卡有响应 | 等 `CMD2/CMD3/CMD9` → `EXT_CSD … SEC_COUNT` |
| `card never left busy (CMD1)` | OCR 一直是 busy 位 | 常见于 CMD1 参数/电压窗口问题 |
| `cmd timeout state=…` | CMD 没响应 | 查 `PRESENT_STATE` 的 CMD_INHIBIT/DAT_ACTIVE 与引脚复用 |

## 3.54 NativeSdhci v9：SDCLK 由 GCC 决定，SDHCI 分频器在 MSM 上是死的 —— 这就是 CMD1 超时的原因（2026-10-27）

### 3.54.1 v8 真机日志（大突破）

```
NativeSdhci[v8]: state=01f800f0
NativeSdhci[v8]: ver=02 caps=742dc8b2 caps1=0000008f
NativeSdhci[v8]: delay probe: begin (cntfrq=19200000 cntpct=ffffffff007b0700)
NativeSdhci[v8]: delay probe: done (advanced=…)
NativeSdhci[v8]: start: asserting software reset (CMD|DATA, 0x2C word)
NativeSdhci[v8]: reset: assert=06 word 000f0007 -> 000f0007
NativeSdhci[v8]: reset: cleared after 0 polls, word=000f0007
NativeSdhci[v8]: start: reset done
NativeSdhci[v8]: bus: hostctl2=0000 word28=0000ff18 (1-bit, 3.3V)
NativeSdhci[v8]: clock: div=24 (400 kHz) word=000e1805 stable
NativeSdhci[v8]: start: clock done
NativeSdhci[v8]: cardInit: CMD0
NativeSdhci[v8]: cardInit: CMD1 (op cond)
NativeSdhci[v8]: cmd error int=0000 err=0001
NativeSdhci[v8]: CMD1 transfer failed (i=0)
NativeSdhci[v8]: card init failed at step 2 (state=01f800f0 err=0000)
```

- `CMD|DATA` 复位立刻就清零（`cleared after 0 polls`），不再卡 200 ms：**§3.53 的改法正确**。
- `delay probe` 两段 32 位打印也对上了（`cntfrq=19200000`）。
- 但 `CMD1` 直接**命令超时**（`err=0001`，Error Interrupt Status bit0 = Command Timeout）。
  注意这次内核没停：`card init failed at step 2` 之后 `NativeSdhci::start()` 正常返回失败，
  后面的 `dlil_init` / `Skylwalk` / `bsd_autoconf` 照常跑。

### 3.54.2 根因：MSM 的 SDHCI 没有内部分频器

`drivers/mmc/host/sdhci-msm.c`（v6.6）里两处原文：

```
/*
 * __sdhci_msm_set_clock - sdhci_msm clock control.
 * MSM controller does not use internal divider and
 * instead directly control the GCC clock as per
 * HW recommendation.
 */
```

以及 `msm_set_clock_rate_for_bus_mode()` 走的是 `clk_set_rate(core_clk, …)`，**不是**写
`SDHCI_CLOCK_CONTROL` 的分频字段。也就是说我们一直写的 `div=24` 完全无效，
真正的 SDCLK = GCC 里 `sdcc1_apps_clk_src` 的输出 = 19.2 MHz。

而 eMMC 在 CMD1 之前，卡的响应是**开漏驱动**（只有上拉 + 卡的下拉），规范要求
此时时钟 ≤ 400 kHz；19.2 MHz 下必然采不到响应 → Command Timeout。
这正好解释了为什么 `state=01f800f0` 里 CARD_INSERTED/CARD_STABLE 都在、CMD 线却是超时而不是别的错。

### 3.54.3 顺带把 RCG 的编码搞清楚

`clk-rcg2.c` 的 `calc_rate()`：

```c
if (hid_div)     rate = rate * 2 / (hid_div + 1);
if (mode)        rate = rate * m / n;          /* mode = CFG[13:12] == 2 即 dual-edge */
```

`__clk_rcg2_configure()` 写寄存器时三个值都取反码：

| 寄存器 | 写入值 |
|---|---|
| `M_REG` | `m` |
| `N_REG` | `~(n - m)` |
| `D_REG` | `~clamp(n, m, 2(n-m))` |
| `CFG_REG` | `hid_div`（bit0..4） + `src_sel`（bit8..10） + `mode`（bit12..13） |

所以 400 kHz（父时钟 XO 19.2 MHz）= `m/n = 1/48`：
`M=0x01`、`N=~47=0xD0`、`D=~48=0xCF`、`CFG=0x2000`（dual-edge、src=XO、不分频）。

### 3.54.4 v9 改动

1. 新增 `setGccSdccRate(m, n, hidDiv)`：关根 → 写 M/N/D 反码 → 写 CFG → `CMD_UPDATE` →
   `CMD_ROOT_EN` → 轮询 `CMD_ROOT_OFF`，最后**按 calc_rate 回读自检**并打印
   `gcc: m/n/hid=… -> … Hz`。
2. `setSdclkDivider()` 换成 `setBusClock(hz)`：按目标频率算出 `m/n`（400 kHz → 1/48，
   19.2 MHz → 1/1），然后 `CLOCK_CONTROL` 只写 `INT_CLK_EN|CARD_EN`、**分频字段恒 0**
   （照 msm 驱动的做法）。`INT_CLK_STABLE` 只记录，不再当致命错误。
3. 识别阶段 400 kHz，`cardInit()` 成功后升到 19.2 MHz（原来写的是 `div=1`）。
4. `CMD1` 参数改成 `0x40FF8000`（HCS + 3.2–3.4V），并且**去掉 `SDHCI_CMD_CRC`** ——
   R3 响应没有 CRC，带 CRC 检查只会误判。

### 3.54.5 判读表（v9）

| 屏幕出现 | 结论 |
|---|---|
| `gcc: m/n/hid=1/48/0 -> 400000 Hz` | GCC 真的输出 400 kHz |
| `clock: 400000 Hz …` | 控制器时钟门已开 |
| `cardInit: OCR=…` | **CMD1 通了，卡在应答** |
| `CMD1 transfer failed (i=0)` + `err=0001` | 还是超时：查 CMD 引脚/上拉、或 GCC 频率没生效 |
| `cmd error … err=0004`（CRC） | 参数/CRC 设置问题（R3 不该带 CRC） |
| `cardInit: CMD2 (cid)` 之后继续 | 识别链走通，只差 CMD3/CMD9/CMD16 |

## 3.55 NativeSdhci v10：400 kHz 真的做到了，CMD1 仍超时 → 转向 vendor 侧模式/IO 电平（2026-10-27）

### 3.55.1 v9 真机日志

```
NativeSdhci[v9]: state=01f800f0
NativeSdhci[v9]: ver=02 caps=742dc8b2 caps1=0000008f
NativeSdhci[v9]: delay probe: begin (cntfrq=19200000 …)  → done
NativeSdhci[v9]: reset: assert=06 word 000f0007 -> 000f0007
NativeSdhci[v9]: reset: cleared after 0 polls, word=000f0007
NativeSdhci[v9]: bus: hostctl2=0000 word28=0000ff18 (1-bit, 3.3V)
NativeSdhci[v9]: gcc: m/n/hid=1/48/0 -> 400000 Hz (cfg=00002000 m=00000001 n=000000d0 d=000000cf)
NativeSdhci[v9]: clock: 400000 Hz (gcc m/n=1/48) int=000e0003 now=000e0005
NativeSdhci[v9]: start: clock done
NativeSdhci[v9]: cardInit: CMD0
NativeSdhci[v9]: cardInit: CMD1 (op cond)
NativeSdhci[v9]: cmd error int=0000 err=0001
NativeSdhci[v9]: CMD1 transfer failed (i=0)
NativeSdhci[v9]: card init failed at step 2 (state=01f800f0 err=0000)
```

- **GCC 那条路是对的**：回读 `cfg=00002000`（dual-edge、src=XO、hid=0）、`m=1`、
  `n=0xd0`（反码 → 48）、`d=0xcf`（反码 → 48），自检算出 `400000 Hz`。
  所以 §3.54 的"SDHCI 分频器无效、频率必须由 GCC 给"这个结论成立，并且已经跑通。
- 但 CMD1 **还是 Command Timeout**（`err=0001`）。也就是说：频率对了、控制器活着、
  复位干净，卡就是不吭声。

### 3.55.2 下一个怀疑对象：vendor 侧的模式与 IO 电平

`sdhci-msm.c` 的 `msm_hc_select_default()`（非 HS400 模式必须做的三件事）：

```c
config = readl(core_vendor_spec);
config &= ~CORE_HC_MCLK_SEL_MASK;        /* bits 8..9 */
config |= CORE_HC_MCLK_SEL_DFLT;         /* 2 << 8 = free running MCLK */
writel(config, core_vendor_spec);

config = readl(core_vendor_spec);
config &= ~CORE_HC_SELECT_IN_EN;         /* bit 18 */
config &= ~CORE_HC_SELECT_IN_MASK;       /* bits 19..21 */
writel(config, core_vendor_spec);
```

同一份代码里 `msm_hc_select_hs400()` 会把 `CORE_HC_MCLK_SEL` 设成 **3**（free running MCLK/2）
并打开 `HC_SELECT_IN`。ABL（LK）在启动 Android 时就是按 HS400/HS200 配的，
**所以我们接手时控制器很可能还停在 HS400 的时钟路径上** ——
在 400 kHz 下走那条路（MCLK/2 + DLL + HC_SELECT_IN）是没有可用时钟输出的，
卡自然一声不响。这解释了为什么"频率读起来是 400 kHz，但 CMD 没有响应"。

IO 电平同理：R11 的 VCCQ 是 1.8V。`sdhci_msm_handle_pwr_irq()` 的逻辑是

```c
if ((io_level & REQ_IO_HIGH) && (caps_0 & CORE_3_0V_SUPPORT))
        new_config &= ~CORE_IO_PAD_PWR_SWITCH;     /* 3.0V */
else if ((io_level & REQ_IO_LOW) || (caps_0 & CORE_1_8V_SUPPORT))
        new_config |= CORE_IO_PAD_PWR_SWITCH;      /* 1.8V */
```

`CORE_IO_PAD_PWR_SWITCH`（VENDOR_SPEC bit16）如果留在 3.0V，控制器的输入门限按 3.0V 判，
卡在 1.8V 上拉回来的高电平会被读成低电平 —— 同样表现为"没有响应"。

### 3.55.3 v10 改动

1. 新增 `configureVendorSpec()`（在 `ver=` 探活之后、`softReset` 之前调用）：
   - 打印 `CORE_MCI_VERSION`(0x318，core major/minor)、`CORE_VENDOR_SPEC`(0x20c)、
     `CORE_VENDOR_SPEC_CAP0`(0x21c)、`CORE_VENDOR_SPEC3`(0x250)、`CORE_MCI_STATUS`(0x324)；
   - `MCLK_SEL = DFLT(2)`，清 `HC_SELECT_IN_EN` 与 `HC_SELECT_IN`；
   - `caps0 & CORE_1_8V_SUPPORT` 时置 `CORE_IO_PAD_PWR_SWITCH`（1.8V）；
   - 清 `CORE_VENDOR_SPEC3` 的 `CORE_PWRSAVE_DLL`(bit3)；
   - 回读并打印 `io18 / mclk_sel / select_in`，屏幕上一眼可查。
2. CMD1 失败不再立刻返回：最多重试 3 次，每次打印
   `CMD1 failed (n) state=%08x norm=%04x err=%04x mstat=%08x`，
   把 `CORE_MCI_STATUS` 也带出来（下次就能看出是"卡没响应"还是"控制器内部状态"）。

### 3.55.4 判读表（v10）

| 屏幕出现 | 结论 |
|---|---|
| `vendor: mci=… (core 1.xx) vs0=… caps0=…` | 能读到 vendor 寄存器，说明映射/memCount=0 的常量没错 |
| `vendor: io18=1 mclk_sel=2 select_in=0` | 模式与 IO 电平已按默认模式配置 |
| `io18=0` | caps0 没报 1.8V 支持，需要手工置 bit16 再试 |
| `cardInit: OCR=…` | **CMD1 通了** |
| `CMD1 failed (n) … err=0001` 三次 | 卡确实没在 CMD 线上应答 → 下一步查供电/引脚/时钟是否真的到卡 |
| `CMD1 failed … err=0004` | CRC/索引错 → 响应有了，参数或标志要修 |

## 3.56 NativeSdhci v10 真机结果：vendor 侧改对了，但 16 位状态读自相矛盾 → v11（2026-10-27）

### 3.56.1 v10 屏幕日志

```
NativeSdhci[v10]: vendor: mci=1000006b (core 1.6b) vs0=00240a0e caps0=742dc8b2 vs3=02626440 mstat=00007600
NativeSdhci[v10]: vendor: io18=1 mclk_sel=2 select_in=0 vs0=00018a0e vs3=02626440
NativeSdhci[v10]: reset: assert=06 word 000f0007 -> 000f0007
NativeSdhci[v10]: bus: hostctl2=0000 word28=0000ff18 (1-bit, 3.3V)
NativeSdhci[v10]: gcc: m/n/hid=1/48/0 -> 400000 Hz (cfg=00002000 m=00000001 n=000000d0 d=000000cf)
NativeSdhci[v10]: cardInit: CMD0
NativeSdhci[v10]: cardInit: CMD1 (op cond)
NativeSdhci[v10]: cmd error int=8001 err=0000
NativeSdhci[v10]: CMD1 failed (1) state=01f800f0 norm=0001 err=0000 mstat=00007600
NativeSdhci[v10]: cmd error int=8001 err=0000
NativeSdhci[v10]: CMD1 failed (2) state=01f800f0 norm=0001 err=0000 mstat=00007600
NativeSdhci[v10]: cmd error int=8000 err=0001
NativeSdhci[v10]: CMD1 failed (3) state=01f800f0 norm=0000 err=0001 mstat=00007600
NativeSdhci[v10]: card init failed at step 2 (state=01f800f0 err=0000)
```

### 3.56.2 读法

- `vendor: mci=…` 说明 vendor 窗口（`0x20c` / `0x21c` / `0x250` / `0x318` / `0x324`）
  **读得到** —— "`memCount=0` 所以用物理常量映射"这条路没问题。
- `io18=1 mclk_sel=2 select_in=0`、`vs0` 从 `0x00240a0e` 变成 `0x00018a0e`：
  **HS400 的时钟路径已经拨回默认模式，IO pad 也切到 1.8V 了**，v10 的目的达到了。
- 但 CMD1 依旧不成功，而且方式变了：
  - 第 1、2 次：`int=8001 err=0000` —— NIS 同时有 **Command Complete(bit0) + Error(bit15)**，
    可 EIS（Error Interrupt Status）读出来是 **0**。这在 SDHCI 里是自相矛盾的：
    bit15 是"任一错误位被置位"的汇总，EIS 不可能为空。
  - 第 3 次：`err=0001` —— 这才是真正的 **Command Timeout**。
  - `mstat=00007600` 三次完全一致，`state=01f800f0` 里 CMD/DATA INHIBIT 都是 0。
- 结论：**16 位访问 `0x30`/`0x32` 这颗控制器上不可靠**（v9 之前已经吃过一次
  "8 位访问把 AHB 挂死"的教训，这次是"16 位读状态读到假值"）。
  必须按 §3.50 定下的规矩：**落在同一个 4 字节对齐字里的多个寄存器，用一次 32 位读写**。
  `0x30`(NIS) 与 `0x32`(EIS) 正好是同一个字，低 16 位 NIS、高 16 位 EIS。

### 3.56.3 v11 改动（commit e736a8a）

1. `waitCommandComplete()` 改成一次 `sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS)`，
   拆成 `nis = word & 0xFFFF`、`eis = word >> 16`，日志改成
   `cmd error word=%08x nis=%04x eis=%04x`。
2. **`eis == 0` 时不再判失败**：汇总位亮而没有具体错误源，按"命令已完成"处理，
   让后续读 RESPONSE / 数据阶段去暴露真实问题（真错的命令在 CMD2/CMD3 会立刻崩）。
3. CMD1 轮询里加 `cardInit: CMD1 resp[%u]=%08x state=%08x`（前 3 次 + 每 100 次一次），
   这样下一张屏幕照片能直接看到 **OCR 到底是不是 0**。

### 3.56.4 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `cmd error word=00010001 nis=0001 eis=0001` | 真正的错误，按 bit 查（bit0=Command Timeout） |
| `cardInit: CMD1 resp[0]=40ff8000`（bit31=0） | **卡应答了！** 只是还在 busy，继续轮询即可 |
| `cardInit: CMD1 resp[0]=c0ff8080` 之类（bit31=1） | 卡就绪，直接进 CMD2 |
| `cardInit: CMD1 resp[0]=00000000` / `ffffffff` | 卡确实没应答 → 转查供电（PMIC vreg）/引脚复用/时钟到卡 |
| `cardInit: OCR=…` 后跟 `CMD2 (cid)` 失败 | 回 CMD 线时序/CRC 标志 |

## 3.57 NativeSdhci v11 真机结果：状态位自相矛盾 → v12 把判定顺序掉过来（2026-10-27）

### 3.57.1 v11 屏幕日志

```
NativeSdhci[v11]: vendor: io18=1 mclk_sel=2 select_in=0 vs0=00018a0e vs3=02626440
NativeSdhci[v11]: reset: cleared after 0 polls, word=000f0007
NativeSdhci[v11]: bus: hostctl2=0000 word28=0000ff18 (1-bit, 3.3V)
NativeSdhci[v11]: gcc: m/n/hid=1/48/0 -> 400000 Hz (cfg=00002000 m=00000001 n=000000d0 d=000000cf)
NativeSdhci[v11]: clock: 400000 Hz (gcc m/n=1/48) int=000e0003 now=000e0005
NativeSdhci[v11]: cardInit: CMD0
NativeSdhci[v11]: cardInit: CMD1 (op cond)
NativeSdhci[v11]: cmd error word=00008001 nis=8001 eis=0000
NativeSdhci[v11]: CMD1 failed (1) state=01f800f0 norm=0001 err=0000 mstat=00007600
NativeSdhci[v11]: cmd error word=00008001 nis=8001 eis=0000
NativeSdhci[v11]: CMD1 failed (2) state=01f800f0 norm=0001 err=0000 mstat=00007600
NativeSdhci[v11]: cmd error word=00008000 nis=8000 eis=0001
NativeSdhci[v11]: CMD1 failed (3) state=01f800f0 norm=0000 err=0000 mstat=00007600
NativeSdhci[v11]: card init failed at step 2 (state=01f800f0 err=0000)
```

### 3.57.2 读法：两条信息

1. **32 位合并读与两次 16 位读的结果完全一致**（`word=00008001` ↔ `norm=0001 err=0000`），
   说明 `0x30`/`0x32` 的位宽本身没读错，v11 的合并读是对的：
   NIS 同时给出 **Command Complete(bit0) + Error 汇总位(bit15)**，而 EIS 是 0。
   三次尝试里只有第 3 次 EIS 真的报了 `0001`（Command Timeout）。
2. 但 v11 的代码逻辑在 `eis == 0` 时应该**落进 RESPONSE 分支返回成功**，
   屏幕上却仍然是 `CMD1 failed`，而且 `resp[0]` 那行一次都没打出来。
   也就是"合上的代码"和"跑出来的行为"对不上 —— 这种时候继续猜没有意义，
   直接把**判定顺序**改成不依赖任何解释：先看 bit0。

### 3.57.3 v12 改动（commit 48130e5）

```c
uint32_t word = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
uint16_t nis = (uint16_t) (word & 0xFFFFu);
uint16_t eis = (uint16_t) (word >> 16);
uint16_t eis16 = sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS);   /* 交叉验证 */

if (nis & SDHCI_INT_RESPONSE) {          /* ← 先判"命令完成" */
        if (nis & SDHCI_INT_ERROR) {
                IOLog ("… cmd done, error flag ignored: word=%08x nis=%04x eis=%04x eis16=%04x\n", …);
        }
        sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, nis);          /* 清掉全部已置位 */
        sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, 0xFFFF);
        return true;
}
if (nis & SDHCI_INT_ERROR) {              /* 只有"没完成又报错"才算失败 */
        IOLog ("… cmd error before complete: word=%08x nis=%04x eis=%04x eis16=%04x\n", …);
        sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, nis);
        sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, 0xFFFF);
        return false;
}
```

另外把 CMD1 的重试上限从 1000 次收到 300 次（每次 10 ms，最长 3 s），
避免卡应答不上时把启动拖十秒。

### 3.57.4 为什么这样改

- `Command Complete` 是控制器自己说"这条命令走完了"，比 `Error` 汇总位更可靠；
  汇总位在没有具体 EIS 位时是幽灵信号（MSM 上已知：真正的错误原因有时只写进
  `CORE_MCI_STATUS`，不写 EIS）。
- 只有 **"命令没完成 + 报错"** 才是真正需要中止的情况。
- 这样一改，无论 EIS 是 0 还是幽灵值，CMD1 都会走到"读 RESPONSE"，
  下一张照片必然能看到 `cardInit: CMD1 resp[0]=????????` 或 `OCR=`。

### 3.57.5 判读表

| 屏幕 | 结论 |
|---|---|
| `cmd done, error flag ignored …` 且随后有 `cardInit: CMD1 resp[0]=40ff8000` | 卡在应答（忙）→ 正常轮询即可 |
| `cmd done, error flag ignored …` 但 `resp[0]=00000000/ffffffff` | 响应寄存器是空的 → 卡没驱动 CMD 线（供电/引脚/时钟到卡） |
| `cmd error before complete: … eis16=0001` | 真的 Command Timeout |
| `card never left busy (CMD1), last=…` | 卡一直忙 → 查 OCR 电压窗/供电 |

## 3.58 NativeSdhci v12 真机结果：**卡应答了**（OCRe=0xc0ff8000），卡在 CMD2（2026-10-27）

### 3.58.1 v12 屏幕日志（里程碑）

```
NativeSdhci[v12]: clock: 400000 Hz (gcc m/n=1/48) int=000e0003 now=000e0005
NativeSdhci[v12]: start: clock done
NativeSdhci[v12]: cardInit: CMD0
NativeSdhci[v12]: cardInit: CMD1 (op cond)
NativeSdhci[v12]: cmd done, error flag ignored: word=00008001 nis=8001 eis=0000 eis16=0000
NativeSdhci[v12]: cardInit: CMD1 resp[0]=40ff8000 state=01f000f0
NativeSdhci[v12]: cmd done, error flag ignored: word=00008001 nis=8001 eis=0000 eis16=0000
NativeSdhci[v12]: cardInit: CMD1 resp[1]=c0ff8000 state=01f000f0
NativeSdhci[v12]: cardInit: OCR=c0ff8000
NativeSdhci[v12]: cardInit: CMD2 (cid)
NativeSdhci[v12]: cmd error before complete: word=00008000 nis=8000 eis=0000 eis16=0000
NativeSdhci[v12]: card init failed at step 3 (state=01f000f1 err=0000)
```

**这是整条存储链第一次真机踩通的关键一步**：

- `resp[1]=c0ff8000` 是实打实的 R3 响应：
  - bit31 `BUSY=1` → 卡已完成初始化（第一次读到的 `40ff8000` 是 busy 还没退出）；
  - bit30 `HCS=1` → 支持 1.8V 高容量；
  - bit23..0 `0xFF8000` → OCR 电压窗 = 3.2–3.4V。
- 也就是说：**GCC 400 kHz + 1-bit 总线 + 开漏 CMD + CMD0/CMD1 全部工作**，
  卡在物理层被我们真正驱动起来了。前面 v3→v11 折腾的时钟、位宽、vendor 模式、
  状态位判定，全是对的。

### 3.58.2 新的卡点：CMD2 不完成、也不报错

- CMD2（ALL_SEND_CID，136 位长响应 + CRC）下发后：
  `word=00008000 nis=8000 eis=0000 eis16=0000`，`state=01f000f1`。
  - `nis=0x8000` 只有 error 汇总位、**没有** Command Complete；
  - EIS 全 0（32 位合并读与 16 位交叉读都是 0）；
  - `state` 的 bit0 `CMD_INHIBIT=1` → 控制器认为命令还在进行中，卡住了。
- 说明 CMD1 虽然"完成"，但硬件里留了一个**错误锁存**（NIS 的幽灵 bit15），
  我们清 NIS 并不足以清掉控制器内部状态，于是下一条命令被卡住。

### 3.58.3 v13 改动（本轮提交）

1. 新增 `recoverCommandState (const char *why)`：先打印 `state/nis/eis/mstat`，
   再写 `0xFFFF` 清 NIS+EIS，然后 `softReset (SDHCI_RESET_CMD | SDHCI_RESET_DATA)`
   （只复位命令/数据状态机，不碰时钟电源），延时 1 ms 后打印复位结果。
2. `cardInit()` 在读 OCR 之后立刻 `recoverCommandState ("after CMD1 (stale error latch)")`，
   把 CMD1 留下的锁存清掉再发 CMD2 —— **这是本轮的主实验**。
3. **CMD2 失败不再致命**：打 `CMD2 failed, continuing without CID` 后 `cid0 = 0` 继续。
   eMMC 的 RCA 由 host 指定，CID 只是信息，所以即使 136 位长响应这条路不通，
   也还能继续 CMD3/CMD9/CMD7，把"到底哪一段不通"暴露出来。
4. CMD3 失败时先 `recoverCommandState` 再重试一次，两次都失败才放弃。

### 3.58.4 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `recover: done (state=01f000f0)` 后 CMD2 成功并出现 `CMD3 (rca=1)` | 幽灵锁存是真的原因，继续走 |
| CMD2 仍卡、但 **CMD3 成功** | 只有 136 位长响应（R2）这条路不通 → 查 RESPONSE 读取宽度/CRC 标志 |
| CMD2 卡 + **CMD3 也卡** | 带 CRC 的命令都不通 → 查 CMD 线 CRC 状态（DAT0-7 无关）或卡仍处于 busy |
| 出现 `CMD9 (csd)` / `CMD7 (select)` / `CMD16 (blklen 512)` | 识别阶段基本完成，下一步读 EXT_CSD、GPT |

## 3.59 NativeSdhci v13 真机结果：R2 长响应卡死、数据阶段超时 → v14（2026-10-27）

### 3.59.1 v13 屏幕日志

```
cardInit: CMD0
cardInit: CMD1 (op cond)
cmd done, error flag ignored: word=00008001 nis=8001 eis=0000 eis16=0000
cardInit: CMD1 resp[0]=40ff8000 state=01f000f0
cardInit: CMD1 resp[1]=c0ff8000 state=01f000f0
cardInit: OCR=c0ff8000
recover: after CMD1 (stale error latch) (state=01f000f0 nis=8000 eis=0000 mstat=00007600)
reset: cleared after 0 polls, word=000e0007
recover: done (state=01f000f0)
cardInit: CMD2 (cid)
cmd error before complete: word=00008000 nis=8000 eis=0000 eis16=0000
cardInit: CMD2 failed, continuing without CID
recover: done (state=01f000f0)
cardInit: CMD3 (rca=1), cid0=00000000
cardInit: CMD9 (csd)
cmd error before complete: word=00008000 nis=8000 eis=0000 eis16=0000
cardInit: CMD7 (select)
cardInit: CMD16 (blklen 512)
start: card init done
gcc: m/n/hid=1/1/0 -> 19200000 Hz (cfg=00000000 m=00000001 n=000000ff d=000000ff)
clock: 19200000 Hz (gcc m/n=1/1) int=000e0003 now=000e0007
data timeout state=01f08af6
EXT_CSD transfer failed
data timeout state=01f08af6
LBA0 read FAILED
media probe failed at step 9
```

### 3.59.2 三条新结论

1. **"幽灵错误锁存"假说被否掉**：CMD1 之后我们做了 `softReset(CMD|DATA)`，
   `recover: done (state=01f000f0)` 干净，CMD2 依然不完成。
2. **失败的命令只有 R2（136 位长响应）这一类**：CMD2（ALL_SEND_CID）和
   CMD9（SEND_CSD）都卡住；而 **CMD3（R1+CRC）、CMD7（R1b）、CMD16（R1）都过了**
   （屏幕上没有任何 CMD3/CMD7/CMD16 的错误行）。48 位响应全部正常，只有 136 位不正常。
3. **数据阶段另有一个独立问题**：升到 19.2 MHz 后 CMD8（SEND_EXT_CSD，512 字节数据）
   走到 `data timeout state=01f08af6`（`DATA_INHIBIT=1`、`DAT_LINE_ACTIVE=1`），
   而且 PIO 的 128 个字都读出来了（没有 `pio read starved`），只是 **DATA_END 位没来**。

### 3.59.3 根因（R2）

R2（136 位响应）的响应帧里 **没有命令索引字段**：136 位 = 起始位 + 传输位 +
6 位命令索引 + 127 位数据……虽然帧里确有索引，但 SDHCI 规范对 R2 的第 4 位
（Command Index Check Enable）要求是 **0**；Linux 的 `mmc_resp_type()`
对 `MMC_RSP_R2` 只给 `MMC_RSP_PRESENT|MMC_RSP_136|MMC_RSP_CRC`，**没有 `MMC_RSP_OPCODE`**，
所以 sdhci 不会设置索引校验位。我们一直带着 `SDHCI_CMD_INDEX`，
控制器就在等一个"索引校验结果"，136 位响应下这个校验不会成立 →
`CMD_INHIBIT` 挂住、不完成、也不报错，与实测完全一致。

### 3.59.4 v14 改动

1. CMD2 / CMD9 的标志改成 `SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC`（去掉 `SDHCI_CMD_INDEX`）。
2. **探测阶段保持 400 kHz**：`start()` 里改成先 `probeMedia()`，成功后再
   `setBusClock (19200000)`；v13 的 19.2 MHz 数据超时很可能只是"太快 + 尚未验证",
   先用最低速把第一次块读跑通。
3. `waitTransferComplete()` 超时日志补上 `nis` 与 `eis`：`data timeout nis=%04x eis=%04x state=%08x`。

### 3.59.5 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `cardInit: CMD2 (cid)` 后出现 `cardInit: CMD3 (rca=1), cid0=…` 且 cid0≠0 | R2 修好了，识别阶段打通 |
| `cardInit: CMD2` 之后再无错误行、直接进 CMD3 | 同上（CID 值本来就可能为 0，看有没有 `cmd error`） |
| `EXT_CSD rev=… SEC_COUNT=…` | **数据读通了**，下一步就是 GPT |
| `data timeout nis=…` 仍出现 | 数据阶段另有问题：按 `nis` 的 DATA_END/DATA_AVAIL 位与 `state` 继续查 |

## 3.60 NativeSdhci v15：R2 修好、CID 读出来了；数据阶段卡在 DMA Select（2026-10-27）

### 3.60.1 v14 屏幕日志

```
cardInit: CMD2 (cid)
cardInit: CMD3 (rca=1), cid0=00150100      <- CID 非 0，且 CMD2/CMD9 一条错都没有
cardInit: CMD9 (csd)
cardInit: CMD7 (select)
cardInit: CMD16 (blklen 512)
start: card init done
start: probing media at 400 kHz
data timeout nis=0020 eis=0000 state=01f00af6
EXT_CSD transfer failed
data timeout nis=0000 eis=0000 state=01f00af6
LBA0 read FAILED
media probe failed at step 9
```

**识别阶段已经完全打通**：CMD0 → CMD1（OCR=c0ff8000）→ CMD2（CID=00150100）→
CMD3 → CMD9 → CMD7 → CMD16 全过，连 400 kHz 都保持在最低速。
R2 的根因（`SDHCI_CMD_INDEX` 用在 136 位响应上）确认。

### 3.60.2 数据阶段的现场解读

`state=01f00af6` 逐位读：

| 位 | 名字 | 值 | 含义 |
|---|---|---|---|
| 1 | DATA_INHIBIT | 1 | 数据线忙 |
| 2 | DAT_LINE_ACTIVE | 1 | 卡还在驱动 DAT0（数据没结束） |
| 4 | WRITE_TRANSFER_ACTIVE | 1 |  |
| 5 | READ_TRANSFER_ACTIVE | 1 | 读传输没结束 |
| 11 | BUFFER_READ_ENABLE | 1 | **控制器认为 FIFO 里还有数据没取** |

而 `nis=0020` 只有 bit5（BUFFER_READ_READY），**没有 bit1（DATA_END）**。
也就是说：我们把 128 个字读完了，控制器却仍然认为还有数据 —— 传输量对不上。

### 3.60.3 根因候选：HOST_CONTROL 的 DMA Select 还是 ADMA2(64-bit)

`bus: hostctl2=0000 word28=0000ff18`：低字节 `0x18` = bit3|bit4 = **DMA Select = 11
（ADMA2, 64-bit addressing）**，这是 ABL 用 DMA 读系统时留下的配置。我们全程走 PIO
（`TRANSFER_MODE` 的 DMA Enable = 0），但 MSM 的 SDCC 在 DMA Select 非 0 时，
数据引擎仍然会使用 `SDHCI_ADMA_ADDRESS(0x58)` 里的描述符地址 —— 而我们从没写过这个寄存器，
里面是 ABL 留下的野值。数据阶段于是"卡在搬运、永不收尾"，与实测完全一致。

### 3.60.4 v15 改动

1. `setBusPowerAndClock()`：`hostCtrl &= ~(0x18u | 0x20u)` —— 清 DMA Select（bits 3-4）
   与扩展 DMA 位（bit5），把控制器按 SDMA/PIO 语义配置。
2. `readExtCsd()` / `readBlocks()` 的 `TRANSFER_MODE` 改用
   `SDHCI_TRNS_READ | SDHCI_TRNS_BLK_CNT_EN`：显式告诉控制器"按 BLOCK_COUNT 收尾",
   避免被当作"开到 CMD12 为止"的多块传输。
3. 数据阶段仪表化：
   * CMD8 之前打 `CMD8 setup blksz=… cnt=… mode=… word28=… attempt=…`（回读四个寄存器）；
   * `pioTransfer()` 读满后打 `pio read %u words state=… nis=… eis=…`；
   * 读 FIFO 饿死时打 `state`/`nis`；
   * `readExtCsd()` 与 `readBlocks()` 都加了"失败就 `recoverCommandState()` 再重试一次"。
4. `kSdhciTag` = `v15`。

### 3.60.5 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `EXT_CSD rev=… SEC_COUNT=…` | 数据读通，下一步直接 GPT |
| `pio read 128 words state=…` 里 bit11 变成 0 且随后成功 | DMA Select 就是根因 |
| `data timeout` 仍出现、`pio read ... state` 的 bit11 仍为 1 | 传输量确实不符：查 BLOCK_SIZE 回读值与 `blkcnt` |
| `pio read starved at word N` | FIFO 供数中断：查 DAT0 线/时钟占空比/`TIMEOUT_CONTROL` |

## 3.61 NativeSdhci v15 真机结果：DMA Select 已清、命令阶段通过，数据阶段依旧没有 DATA_END（2026-10-27）

### 3.61.1 屏幕日志（v15）

```
cardInit: CMD2 (cid)
cardInit: CMD3 (rca=1), cid0=00150100
cardInit: CMD9 (csd)
cardInit: CMD7 (select)
cardInit: CMD16 (blklen 512)
start: card init done
start: probing media at 400 kHz
bus: hostctl2=0000 word28=0000ff00 (1-bit, 3.3V)     <- 低字节 0x18 已清成 0x00
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff00 attempt=0
                                                      <- 之后屏幕停在光标，无后续行
```

### 3.61.2 已确认

1. **DMA Select 清零生效**：`word28` 从 `0000ff18` 变成 `0000ff00`。
2. `blksz=0200`、`cnt=0001`、`mode=0012`（READ|BLK_CNT_EN）三个寄存器回读值都正确。
3. CMD8 之后的 `CMD8 ok state=…` 行没有出现 —— 说明卡在 `sendCommand(CMD8)` 里，
   即**命令阶段**（`CMD_INHIBIT` 等待或 `waitCommandComplete`）而不是数据阶段。

### 3.61.3 v16 改动

1. `kSdhciDataLoops` 4000000 → **400000**（4 s → 0.4 s）：4 秒太长，屏幕上看不到
   超时行就以为卡死了。
2. `readExtCsd()` 在 `sendCommand` 成功后立刻打
   `CMD8 ok state=%08x nis=%08x eis=%08x adma=%08x` —— 把"命令阶段结束"与
   "数据阶段开始"这两件事在日志上彻底分开，并顺带确认 ADMA_ADDRESS 为 0。
3. `pioTransfer()` 在读 128 个字的过程中按 `word 0 / 32 / 64 / 96 / 127` 采样打印
   `state`/`nis`，看清 FIFO 是"一次给满"还是"逐字滴出"。
4. `kSdhciTag` = `v16`。

### 3.61.4 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `CMD8 ok state=…` 出现在 `CMD8 setup` 之后 | 命令阶段通过，问题在数据阶段 |
| `CMD8 setup` 之后直接 `CMD8 (SEND_EXT_CSD) failed` | 命令阶段超时：查 CMD 线/`CMD_INHIBIT` |
| `pio word 0 state=…` 后 `pio read starved at word 1` | 只有 1 个字出来：DAT0 上有数据但没持续 |
| `pio read 128 words state=…` 且 bit11 为 0 | 数据搬完了，接下来只差 DATA_END |
| `EXT_CSD rev=8 SEC_COUNT=…` | 数据阶段通了 |

## 3.62 NativeSdhci v16 真机结果：清 DMA Select 反而更糟 —— 命令阶段就挂死（2026-10-27）

### 3.62.1 屏幕日志（v16，等了 7 分钟仍是这一屏）

```
start: probing media at 400 kHz
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff00 attempt=0
                                                        <- 没有 "CMD8 ok"，也没有 "CMD8 failed"
```

### 3.62.2 结论：v15 的"清 DMA Select"是负优化

| 版本 | word28 | 现象 |
|---|---|---|
| v13 / v14 | `0000ff18`（DMA Select = ADMA2-64） | 命令阶段通过，数据阶段 `data timeout nis=0020` |
| v15 / v16 | `0000ff00`（DMA Select = SDMA） | **CMD8 一发出就整机静默** |

也就是说，MSM 的 SDCC 在 `DMA Select = 00`（SDMA）时会**真的启动 SDMA 引擎**，
拿 `SDHCI_ADMA_ADDRESS(0x58)` 里的野值（ABL 留下的地址）去做总线搬运，直接把 AHB 挂死。
而 `DMA Select = 11`（ADMA2）时代码虽然没配描述符，控制器反而没动手。
这与"我们没有配置任何 DMA 缓冲区"的处境一致：**既不能开 DMA，也不能把选择位拨到 SDMA**。

### 3.62.3 v17 改动

1. **恢复** `HOST_CONTROL` 的 DMA Select（不再清零），改为打印
   `bus: hostctl=18 (DMA select=3, keep as-is)`。
2. 每次**数据命令**之前把 `SDHCI_ADMA_ADDRESS(0x58)` 清零，并打
   `cmd%u pre state=%08x adma=%08x` —— 排除"野指针"这条通路。
3. `sendCommand()` 给数据命令加三段探针：
   * `cmd%u pre state=…`（等 inhibit 之后、写寄存器之前）
   * `cmd%u posted state=…`（刚写完 COMMAND 寄存器）
   * `cmd%u complete state=…`（命令完成中断之后）
   这三行能精确区分"写 COMMAND 挂死"、"命令永不完成"、"命令完成但数据不开始"。
4. `kSdhciTag` = `v17`。

### 3.62.4 下一刷的判读表

| 屏幕最后一行 | 结论 |
|---|---|
| `cmd8 pre state=…` 之后停住 | 写 NIS/EIS/ARGUMENT/COMMAND 其中之一挂死（AHB 层面） |
| `cmd8 posted` 之后停住 | 控制器接受命令后立刻挂死：数据引擎被启动（DMA/电源/时钟门控） |
| `cmd8 complete` 之后停住 | 命令完成，卡在数据 FIFO（回到 v14 的问题） |
| `CMD8 ok … adma=00000000` | 命令与野指针都排除了，问题纯粹在数据通路 |
| `EXT_CSD rev=8 SEC_COUNT=…` | 通了 |

## 3.63 NativeSdhci v17 真机结果：静默是我们自己的错位 32 位读造成的（2026-10-27）

### 3.63.1 屏幕日志（v17）

```
bus: hostctl=18 (DMA select=3, keep as-is)
bus: hostctl2=0000 word28=0000ff18 (1-bit, 3.3V)
...
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff18 attempt=0
cmd8 pre  state=01f000f0 adma=00000000
cmd8 posted state=01f000f1
cmd8 complete state=01e002f6
                                        <- 之后再无任何输出（整机静默）
```

### 3.63.2 定位

`cmd8 complete` 打印之后，下一行本该是 v16 加的
`CMD8 ok state=… nis=… eis=… adma=…`，它的四个参数里最后两个是
`sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS)` —— 而 **`SDHCI_ERROR_INT_STATUS = 0x32`，
`0x32 % 4 == 2`，这是错位 32 位读**。Snapdragon 的 AHB 从口不拆错位事务，
于是事务不完成、CPU 停在 `ldr` 上，机器静默、无 panic、无日志。**这一屏的"挂死"
是日志语句本身造成的，不是驱动逻辑。**

这与 §3.50 的 `0xFE` 错位读是同一个坑：**任何 32 位访问都必须落在 4 字节对齐的偏移上**。

### 3.63.3 附带收获

`cmd8 complete state=01e002f6` 本身证明命令阶段是通的：
`DATA_INHIBIT=1`、`DAT_LINE_ACTIVE=1`、`READ_TRANSFER_ACTIVE=1` —— 控制器已经在
驱动 DAT0 收数据了。也就是说 v14 之后"数据阶段不启动"这个担心可以排除了。

### 3.63.4 v18 改动

1. 两处 `sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS)` → `sdhciRead16 (...)`（0x32 是 16 位对齐）。
2. 新增静态检查脚本 `E:\DeepSeekWorkspace\out\check-alignment.py`：
   解析 `SdhciRegs.h` 的偏移表，扫描 `NativeSdhci.cpp` 里所有
   `sdhciRead/Write{8,16,32}(fCore, NAME)`，按 `offset % (width/8) == 0` 校验，
   当前输出 **错位访问: 无**（99 个寄存器定义、30 个 32 位访问点全部对齐）。
3. `kSdhciTag` = `v18`。

### 3.63.5 下一刷的判读表

| 屏幕 | 结论 |
|---|---|
| `CMD8 ok state=… nis=… eis=… adma=00000000` | 命令阶段通过、野指针排除 |
| `pio word 0/32/64/96/127 state=…` 都有 | FIFO 持续供数，数据通路正常 |
| `pio read 128 words state=…` 且 bit11=0 | 数据搬完，只差 DATA_END |
| `pio read 128 words state=…` 且 bit11=1 | 卡在送更多的数据（传输量不符：查 BLOCK_SIZE/CMD16） |
| `pio read starved at word N` | FIFO 断供（DAT0 信号质量/时钟/供电） |
| `EXT_CSD rev=8 SEC_COUNT=…` | **数据阶段通了** |

## 3.64 NativeSdhci v18 真机结果：数据真的在流，但 DATA_END 永远不来（2026-10-27）

### 3.64.1 屏幕日志（v18，CMD8 两轮 + CMD17 两轮，形态完全一致）

```
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff18 attempt=0
cmd8 pre  state=01f000f0 adma=00000000
cmd8 posted state=01f000f1
cmd8 complete state=01e002f6
CMD8 ok state=01e002f6 nis=00000020 eis=00000000 adma=00000000
pio word 0   state=01f00af6 nis=00000020
pio word 32  state=01f00af6 nis=00000020
pio word 64  state=01f00af6 nis=00000020
pio word 96  state=01f00af6 nis=00000020
pio word 127 state=01f00af6 nis=00000020
pio read 128 words state=01f00af6 nis=00000020 eis=00000000
data timeout nis=0020 eis=0000 state=01f00af6
EXT_CSD transfer failed (attempt 0)
...（attempt 1 完全相同）
cmd17 ...（LBA0 读，两轮，形态完全相同）
LBA0 read FAILED
media probe failed at step 9
```

### 3.64.2 这一屏确认的事

1. **命令阶段完全正常**：`cmd8 pre/posted/complete` 三行都在，`adma=00000000`。
2. **数据确实在进 FIFO**：`nis=0x20` 就是 NIS bit5 = Buffer Read Ready，
   `state` 里 `READ_TRANSFER_ACTIVE(bit9)=1`、`DAT_LINE_ACTIVE(bit2)=1`。
3. **128 个字（512 B）读完，`BUFFER_READ_ENABLE(bit11)` 仍然是 1** —— 控制器认为
   还有数据没取；`DATA_END(NIS bit1)` 从头到尾没有出现过。
4. 400 kHz 下 CMD17 与 CMD8 的表现一模一样 → 与"哪个命令"无关，
   是**数据阶段的收尾条件**没满足。

### 3.64.3 v19 改动：不再猜块长，直接量

`pioTransfer()` 的读路径改成"有多少收多少"：

* 循环条件不再固定 `count`，而是"FIFO 里还有数据（bit11）就继续读"，上限 4 倍块长；
* 打完第一轮就打 `pio got %u words (block=%u) first=… last=…` ——
  **这条直接把真实传输量量出来**：128 就是正常一块，256/512 就是块长不符，
  4096 就是"开到 CMD12 为止"的多块；
* 再打 `pio after state=… nis=… eis=…`，看收尾位；
* `first=` 的前四个字还能顺便验证数据是不是真的（LBA0 应该看到 MBR/GPT 的内容，
  全 0 就说明 FIFO 给的是空/垃圾）。

### 3.64.4 下一刷的判读表

| `pio got` 行 | 结论 | 下一步 |
|---|---|---|
| `got 128 words` 且 `after` 里 bit11=0 | 数据量正确、只是 DATA_END 位没置 | 直接改用"FIFO 空 + 不再有新数据"当收尾条件 |
| `got 128 words` 且 bit11=1 | 控制器还想继续收 | 查 BLOCK_SIZE/CMD16/CSD 的 READ_BL_LEN |
| `got 256/512 words` | 卡按 1/2 KB 块在发 | CMD16 没生效，改用 CMD6 改 EXT_CSD 的块长 |
| `got 4096 words`（顶到上限） | 多块开环传输 | 需要 CMD12 收尾 |
| `first=00000000 00000000 …` | FIFO 给的是空数据 | 时钟/采样点问题，回到 400 kHz 以下继续降速验证 |

## 3.65 NativeSdhci v19 真机结果：512 个字全是 0 → CPU 读 FIFO 这条路不通，v20 改 ADMA2（2026-10-27）

### 3.65.1 v19 屏幕日志（CMD8 两轮 + CMD17 两轮）

```
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff18 attempt=0
cmd8 pre  state=01f000f0 adma=00000000
cmd8 posted state=01f000f1
cmd8 complete state=01e002f6
CMD8 ok state=01e002f6 nis=00000000 eis=00000000 adma=00000000
pio got 512 words (block=128) first=00000000 00000000 00000000 00000000 last=...
pio after state=01f00af6 nis=00000020 eis=00000000
data timeout nis=0020 eis=0000 state=01f00af6
EXT_CSD transfer failed (attempt 0)
...（attempt 1 相同）
cmd17 ...（LBA0，两轮相同）
LBA0 read FAILED
media probe failed at step 9
```

### 3.65.2 三条硬结论

1. **FIFO 端口被 CPU 读出来是 0，而且读 512 个字也不清 `BUFFER_READ_ENABLE(bit11)`**
   —— 说明这条"CPU 直读 Buffer Data Port"的 PIO 路径在这块 MSM 控制器上不成立：
   读不会推进 FIFO 指针（`first` 是四个 0，`bit11` 依旧为 1）。
2. **错误寄存器全程为 0**（`eis=0000`），`DATA_END(bit1)` 从不出现 —— 控制器不认为
   传输失败，只是永远不认为结束。这与"FIFO 交接由 DMA 引擎负责、CPU 侧读的是空壳"
   的形态一致：ABL 交接时 `Host Control 1` 的 DMA Select 就是 `11`（ADMA2-64）。
3. `cmd8 complete state=01e002f6` 说明命令阶段本身没问题，卡也确实开始发数据了
   （`READ_TRANSFER_ACTIVE=1`、`DAT_LINE_ACTIVE=1`）。

### 3.65.3 v20：改用 ADMA2（控制器原生的搬运方式）

新增（`drivers/NativeSdhci/NativeSdhci.cpp`）：

* 成员：`fDmaDesc` / `fDmaData`（`IOBufferMemoryDescriptor::withCapacity(..., kIODirectionOutIn, true)`，
  物理连续）、`fDmaDescPhys` / `fDmaDataPhys`；
* `setupDmaMemory()`：`start()` 里时钟就绪后、`cardInit()` 之前分配 2 页连续内存并取物理地址；
* `beginDmaRead(bytes)`：填一条 64 位 ADMA2 描述符
  （`word0 = (bytes<<16) | 0x27`（Valid|End|Int|TRAN）、`word1/2 = 数据物理地址低/高`），
  `prepare()` 后把描述符物理地址写进 `SDHCI_ADMA_ADDRESS(0x58)`；
* `endDmaRead(dst, bytes)`：`waitTransferComplete()` → 打印
  `dma: done ok=… state/nis/eis/adma/err(0x214,0x218)` 与
  `dma: data 前4字 | w52 w53(SEC_COUNT) w126 w127` → `complete()` → `memcpy` 给调用方；
* `readExtCsd()` / `readBlocks()`：`TRANSFER_MODE` 置 `SDHCI_TRNS_DMA | READ | BLK_CNT_EN`，
  命令前 `beginDmaRead()`、命令后 `endDmaRead()`；`pioTransfer()` 保留给写路径。

### 3.65.4 下一刷的判读表

| `dma: done` / `dma: data` | 结论 |
|---|---|
| `ok=1` 且 `data` 里 `w53` 非 0 | ADMA2 通了 → `EXT_CSD rev/SEC_COUNT` 会紧跟着打出来，LBA0 也能读到 |
| `ok=1` 但 data 全 0 | DMA 完成但内容空 → 查描述符长度/属性、以及 `CORE_VENDOR_SPEC` 的 DMA 位 |
| `ok=0` 且 `err=…` 指向描述符地址 | ADMA 取描述符失败（地址/对齐/缓存可见性） |
| `ok=0` 且 `eis` 出现 `ADMA(bit9)` | 描述符链本身有问题 |
| `dma: desc va=… phys=00000000` | `withCapacity` 拿不到物理地址，需要换 `IOMallocContiguous` 方案 |

## 3.66 上屏补丁不能再用固定地址：CI 导出符号表，补丁脚本按符号定位（2026-10-27）

v20 刷机后内核零输出、屏幕停在 UniXNU 菜单帧。原因是 `patch-init-screen-gc.py`
里两个 VA 是写死的：

```python
RET_VA     = 0xFFFFFFF0073BA7C8   # initialize_screen 的 ret
GC_ENABLE_VA = 0xFFFFFFF0073BBC40 # gc_enable 的入口
```

v20 那轮内核重新链接后，`0x73BA7C8` 落在 `initialize_screen` 中间（内容是
`ldr w8, [x19]` = `0xb9400268`），脚本打不上、直接失败：

```
空洞: 0xfffffff0079f1444
0xfffffff0073ba7c8: 0xb9400268 (期望 0xd65f03c0)
!! 指令不匹配
```

PAN / CPU 校验 / 输出开关 / serial_init / cons_ops_index 那些按指令编码找的补丁
全部成功，只有这条按地址找的失败 —— 于是内核日志无处可去，屏幕就停在菜单帧。
诊断确认内核本身是新的（`grep -c 'NativeSdhci\[v20\]' kernel.macho` = 93）。

### 3.66.1 改法

1. CI 在 `编译内核` 之后多跑一步，用 `llvm-nm` 把符号表导出到
   `UniXNU/out/r11/kernel/kernel-symbols.txt`（已在 `r11-boot-artifacts` 的上传
   清单里，跟着 artifact 一起下载）：

   ```bash
   NM=$(xcrun -f llvm-nm)
   K=UniXNU/out/r11/tree/build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837.unstripped.noctf
   "$NM" --defined-only "$K" | awk 'NF>=3 {print $1, $2, $3}' > UniXNU/out/r11/kernel/kernel-symbols.txt
   ```

2. `patch-init-screen-gc.py` 重写成符号驱动：
   * 必须传 `--symbols <nm.txt>`，固定地址模式删除；
   * 在 `_PE_init_platform` 的起止范围内扫 `bl`（`(ins & 0xFC000000) == 0x94000000`，
     `imm` 手动符号扩展），命中 `target == _initialize_screen` 的就是调用点；
     找不到再退回扫整个 `__TEXT_EXEC`；
   * 在 `__TEXT_EXEC` 的 NOP 空洞里写 6 条指令的跳板（24 字节）：

     ```
     stp  x29, x30, [sp, #-16]!
     bl   _initialize_screen
     mov  w0, #1
     bl   _gc_enable
     ldp  x29, x30, [sp], #16
     ret
     ```

     然后把调用点那条 `bl _initialize_screen` 改成 `bl 跳板`。

   与旧做法的区别：旧法在 `initialize_screen` 的尾声把 `ret` 换成跳转到
   `gc_enable` 再返回，需要知道尾声地址；新法"包一层"，只依赖两个符号，语义
   完全等价（仍然是 `initialize_screen` 返回之后、单线程、vinfo 完整）。

3. `patch-payload-full.py` 增加 `--symbols`，并会在这些位置自动找符号表：
   `/root/src/UniXNU/out/r11/kernel/kernel-symbols.txt`、payload 同目录、
   `../kernel/kernel-symbols.txt`、`/tmp/art*/kernel/kernel-symbols.txt`。
   找不到就明确报错，不再静默退化成"没打上屏补丁"。

### 3.66.2 自测

`E:\DeepSeekWorkspace\out\test-gc-symbols.py`：从真实内核里挑一条 `bl`，把它的
目标当成 `_initialize_screen` 造符号表，跑脚本后检查

* 命中同一条 `bl`；
* 该处被改成 `bl 0xfffffff0079ec9a0`（跳板）；
* 跳板 6 条指令 `a9bf7bfd 97e2356f 52800020 97e2366d a8c17bfd d65f03c0`，
  其中两条 `bl` 的位移解出来正好是 `_initialize_screen` 与 `_gc_enable`。

### 3.66.3 教训

补丁脚本里只要出现"某个函数的尾部/某个不在 4 字节边界的寄存器"这类地址，就必须
改成按符号或按指令编码定位 —— 内核每次重新链接，这些 VA 都会漂。

### 3.66.4 最终实现：挂尾声 ret（20 字节跳板），不再改调用点

第一版按调用点改：扫 `bl _initialize_screen`，把它拐到跳板。在真内核上一试就
发现问题 —— 有 **6 处** 调用（`0x73c06dc`、`0x794132c`、`0x7941354`、`0x794140c`、
`0x7941d78`、`0x79896fc`），只改第一处等于只在某个调用者上生效，语义不对。

改成挂函数尾声，和旧版行为一致、但地址是算出来的：

1. 函数范围 = `[_initialize_screen, 下一个符号)`；这一版是
   `0xfffffff0073bdec8 .. 0xfffffff0073be878`（0x9b0 字节）；
2. 在范围里找 `ret`（`0xD65F03C0`）取最后一个 —— 这一版恰好只有 1 个：
   `0xfffffff0073be7c8`；
3. 跳板 5 条（20 字节）：`stp x29,x30,[sp,#-16]!` / `mov w0,#1` /
   `bl _gc_enable` / `ldp x29,x30,[sp],#16` / `ret`；
4. 把尾声那条 `ret` 换成 `b 跳板`。

真机内核上的实际结果（WSL 里用 `llvm-nm` 现导符号表验证）：

```
符号: initialize_screen=0xfffffff0073bdec8 gc_enable=0xfffffff0073bfc40 函数结束=0xfffffff0073be878
  函数体里 1 个 ret，用最后一个 0xfffffff0073be7c8
空洞: 0xfffffff0079f1444
  跳板写入 20 字节
  0xfffffff0073be7c8: ret -> b 跳板(0x1418cb1f)
```

反解校验：`b` 落到 `0xfffffff0079f1444`，跳板是
`a9bf7bfd 52800020 97e739fd a8c17bfd d65f03c0`，其中 `bl` 的目标正是
`_gc_enable = 0xfffffff0073bfc40`。

顺带一个好消息：内核 Mach-O **自带符号表**（`llvm-nm --defined-only` 出 70148 行），
所以 WSL 里不必等 CI 的 `kernel-symbols.txt`：

    llvm-nm --defined-only kernel.macho | awk 'NF>=3 {print $1, $2, $3}' | sort > kernel-symbols.txt

`patch-payload-full.py` 现在会自己做这件事 —— 找不到符号表就用 `llvm-nm` 从
卷里取出的内核现导一份，实在没有才报错。

又一个教训：`xcrun` 是 macOS 的东西，WSL 里没有（`/usr/bin/xcrun` 是个会报
`exec: -f: invalid option` 的壳），本地一律用 `llvm-nm`。

## 3.67 真正的元凶：`patch-kernel-minimal.py` 里 6 个硬编码地址也漂了（2026-10-27）

上一刷（v20 内核 + 全套补丁）屏幕停在 `iBoot Stage 2 version:` 就死了。查下来
不是 v20 的 ADMA2 代码有问题，而是**补丁打错了地方**。

用 `llvm-nm` 的符号表（70148 行）一对，脚本里写死的地址在 2026-10-27 这版内核里
全部落在别的东西上：

| 常量 | 硬编码值 | 实际落在 | 正确地址（符号） |
|---|---|---|---|
| `SYM_DISABLE[0..2]` | `0x71F9B50/51/52` | `__ZTV17IOPowerConnection+0x1c8`（**C++ vtable**） | `_disable_serial_output 0x71FDB50`、`_disable_kprintf_output 0x71FDB51`、`_disable_iolog_serial_output 0x71FDB52` |
| `SYM_SERIAL_INIT` | `0x793E724` | `__GLOBAL__sub_I_IOPMrootDomain.cpp+0x64` | `_serial_init 0x79424DC` |
| `SYM_CONS_OPS_INDEX` | `0x7167920` | `_zone_security_array+0x998` | `_cons_ops_index 0x716B920` |

后果：把 3 个 vtable 字节清零、把某个静态构造函数的头两条指令改成
`mov w0,#0; ret`、往 zone 安全数组里写了 1。内核随即在
`iBoot Stage 2 version:` 之后（正好开始跑 C++ 静态构造 / IOPMrootDomain）死掉，
连 panic 都打不出来。

这也解释了以前为什么一直没事：**那些内核的布局恰好与常量吻合**。`-v serial=3`
那几轮能看到 `cons_ops_index: 1 -> 1`，就是因为值碰巧一样；一旦内核重新链接，
`2 -> 1` 其实是"往别处写了 1"。

### 3.67.1 改法

`patch-kernel-minimal.py` 现在必须带 `--symbols`：

* `_serial_init` / `_cons_ops_index` / 三个 `_disable_*` 全部按符号定位；
* 找不到符号就**跳过并告警**，绝不退回硬编码地址（那只会改坏别人的 vtable）；
* CPU 校验点保留 `0x49871A0`（在 `__PLK_TEXT_EXEC` 里，prelink 段地址稳定），
  但加了内容断言：读出来不是 `0x12000260`（`and w0, w19, #1`）就跳过；
* PAN / `mrs xN, PAN` / `0xE7FFDEFF` 本来就是全域按编码扫描，不受影响。

`patch-payload-full.py` 改成**先**拿符号表、再依次喂给两个补丁脚本。

### 3.67.2 验证（v20 内核，WSL）

```
1) PAN 指令: 24 条 → nop
2) CPU 校验返回值 @0xfffffff0049871a0: 0x12000260 -> mov w0,#1
3a) mrs xN,PAN → mov wN,#1: 14 处
3b) 0xE7FFDEFF trap → nop: 1 处
   _disable_serial_output @0xfffffff0071fdb50 -> 0
   _disable_kprintf_output @0xfffffff0071fdb51 -> 0
   _disable_iolog_serial_output @0xfffffff0071fdb52 -> 0
4) 输出开关: 3/3 置 0
5) _serial_init @0xfffffff0079424dc: 0xd10183ff -> mov w0,#0; ret
6) _cons_ops_index @0xfffffff00716b920: 1 -> 1（视频控制台）
```

回读校验：`_serial_init` 开头变成 `00008052 c0035fd6`（`mov w0,#0; ret`）、三个
`_disable_*` 各 1 字节为 0，而 `0x71F9B50`（vtable）保持原值 —— 说明这次没有误伤。

### 3.67.3 教训（第二次栽同一类坑）

"screencode 里写死的函数尾声地址"和"写死的全局变量地址"是同一个病。凡是
`0xFFFFFFF00xxxxxxx` 这种常量，只要内核重编就会漂。规则：

* 能按**符号**定位的（nm 里有名字）→ 一律符号；
* 不能按符号定位的（编译器生成的尾声、prelink 里的无名点）→ 按**指令编码 +
  上下文断言**定位，断言不过就跳过，绝不盲改；
* 补丁脚本必须打印"改之前是什么、改完是什么、这个地址属于哪个符号"，出问题
  第一眼就能看出来。

## 3.68 v20 黑屏的真凶：sendCommand() 把 ADMA 描述符地址清零了（2026-10-27，v21）

补丁修好之后内核终于跑到了驱动，v20 的日志是：

```
NativeSdhci[v20]: dma: desc va=0xfffffff7a5a01d000 phys=afdb2000 len=4096 / data va=0xfffffff7a5065000 phys=aff42000 …
NativeSdhci[v20]: cardInit: CMD0 / CMD1 (op cond) / OCR=c0ff8000 / CMD2 / CMD3 / CMD9 / CMD7 / CMD16
NativeSdhci[v20]: start: card init done
NativeSdhci[v20]: start: probing media at 400 kHz
NativeSdhci[v20]: CMD8 setup blksz=0200 cnt=0001 mode=0013 word28=0000ff18 attempt=0
NativeSdhci[v20]: dma: armed bytes=512 desc0=02000027 aff42000 00000000 adma=afdb2000
NativeSdhci[v20]: cmd8 pre state=01f000f0 adma=00000000     <-- 描述符地址被抹掉了
（之后黑屏）
```

`dma: armed` 里 `adma=afdb2000` 是我们写进去的描述符物理地址，
下一行 `cmd8 pre … adma=00000000` 说明它被清掉了。元凶是 v17 加的这条：

```c
if (isData) {
    /* v17：MSM 的 SDMA/ADMA 地址寄存器在 ABL 之后是野值，保险起见清零 */
    sdhciWrite32 (fCore, SDHCI_ADMA_ADDRESS, 0);
    …
}
```

那是 PIO 时代为了防野指针写的。v20 起读路径改走 ADMA2，`beginDmaRead()`
已经把描述符地址写进 `0x58`，`sendCommand()` 再清零 → 控制器拿 NULL 描述符去
搬数据 → 数据阶段直接挂死，屏幕黑掉。

### 3.68.1 v21 改动

* 新增成员 `bool fDmaArmed = false;`（`beginDmaRead()` 置位、`endDmaRead()` 清位）；
* `sendCommand()` 里改成：

  ```c
  if (!fDmaArmed) {
      sdhciWrite32 (fCore, SDHCI_ADMA_ADDRESS, 0);
  }
  ```

  即"PIO 路径照旧清零，DMA 路径不许碰"；
* `kSdhciTag` = `v21`。

### 3.68.2 这轮的收获（进度盘点）

| 里程碑 | 状态 |
|---|---|
| GCC 时钟（`sdcc1_apps_clk_src` M/N/D + 分支） | ✅ v9 起稳定 |
| 控制器探活（`ver=02 caps=742dc8b2`） | ✅ v5 起稳定 |
| CMD0 / CMD1（`OCR=c0ff8000`）/ CMD2 / CMD3 / CMD9 / CMD7 / CMD16 | ✅ v14 起稳定 |
| 400 kHz 识别时钟（GCC 1/48） | ✅ v9 起稳定 |
| 数据阶段（EXT_CSD / LBA0） | 🚧 v20 首次用 ADMA2 真武装，被 sendCommand 清零打断 → v21 修 |
| 块设备注册 / IOMedia | ⬜ 需 15 个 `IOStorageFamily` 符号的 `.set` 绑定 |
| APFS 根卷 / 真 launchd | ⬜ |

也就是说：**eMMC 卡识别这条路已经全通了**，只剩"把 512 字节数据搬进内存"这一步。

## 3.69 v21 修好之后：ADMA2 条目布局错了（64 位要 16 字节条目）→ v22

v21 真机日志，前三行就说明 v21 生效了：

```
CMD8 setup blksz=0200 cnt=0001 mode=0013 word28=0000ff18 attempt=0
dma: armed bytes=512 desc0=02000027 aff2d000 00000000 adma=afd9e000
cmd8 pre state=01f000f0 adma=afd9e000          <-- 描述符地址这次没被清零
cmd8 posted state=01f000f1
cmd done, error flag ignored: word=02000001 nis=0001 eis=0200 eis16=0200
cmd8 complete state=01f002f6
CMD8 ok state=01f002f6 nis=00000000 eis=00000000 adma=afd9e024
data timeout nis=0000 eis=0000 state=01f002f6
dma: done ok=0 state=01f002f6 nis=00000000 eis=0000 adma=afd9e024 err=00000000/00000000
dma: data 00000000 00000000 00000000 00000000 | w52=00000000 w53=00000000 …
```

`eis=0x0200` 是 **EIS bit9 = ADMA Error**（命令阶段的错误位），
`adma=afd9e024` 说明控制器真的开始走描述符表了，但搬出来全是 0。

### 3.69.1 根因

ABL 交接时 `Host Control 1 = 0x18` → **DMA Select = 3 = ADMA2 64 位**。
64 位 ADMA 的条目是 **16 字节**：

```
word0 = attribute(16) | length(16)
word1 = 保留（4 字节）
word2 = address[31:0]
word3 = address[63:32]
```

而 v20/v21 一直按 32 位的 **8 字节** 格式写：

```
word0 = attribute|length
word1 = address[31:0]      <- 控制器在 64 位模式下把这里当"保留字"
```

于是控制器算出来的真正地址是 `desc[2..3] = 0` —— 它去往物理地址 0 搬数据，
ADMA Error，数据自然是全 0。

### 3.69.2 v22 改动

* `beginDmaRead()` 先清 16 个字（避免读到上一次的残留条目），
  再读 `Host Control 1` 的 `DMA Select`：
  * `3` → 写 16 字节的 64 位条目（保留字补 0，地址放 `desc[2]/desc[3]`）；
  * 其它 → 保持 8 字节的 32 位条目。
* `dma: armed` 日志改成
  `layout=%s select=%u bytes=%u desc0=… adma=…`，一眼能看出走的是哪种布局。
* `kSdhciTag` = `v22`。

### 3.69.3 这一刷的判据

| 日志 | 结论 |
|---|---|
| `layout=64 select=3`，随后 `dma: done ok=1` + `dma: data` 有非 0 字 | ADMA2 通了 |
| `layout=64` 但仍 `eis=0200` | 描述符地址/长度还有细节不对（看 `err=…`） |
| `dma: done ok=1` 但 `dma: data` 全 0 | 地址对了但读的是别的内存，查 `fDmaDataPhys` 与 IOMMU |
| 出现 `EXT_CSD rev=… SEC_COUNT=…` | 64 GB 容量第一次被内核读出来 |

## 3.70 ADMA2 这块控制器根本没广告 → 改走 SDMA（v23）

v22 把条目换成 16 字节的 64 位格式，日志是：

```
dma: armed layout=64 select=3 caps=… bytes=512 desc0=02000027 00000000 aff45000 00000000 adma=afdac000
cmd8 pre state=01f000f0 adma=afdac000
cmd done, error flag ignored: word=02000001 nis=0001 eis=0200 eis16=0200
CMD8 ok state=01f002f6 nis=00000000 eis=00000000 adma=afdac024
data timeout nis=0000 eis=0000 state=01f002f6
dma: done ok=0 ... err=00000000/00000000
dma: data 00000000 00000000 00000000 00000000 | w52=… w53=…
```

条目布局对了，`eis=0x200`（ADMA Error）却一次都没变。回头去看 CAPABILITIES：

```
ver=02 caps=742dc8b2 caps1=0000008f
0x742dc8b2 = 0111 0100 0010 1101 1100 1000 1011 0010
  bit28 = 1   64 位系统总线支持
  bit27 = 0   ADMA2 不支持     <-- 就在等这一条
  bit26 = 1   SDMA 支持
```

**这块 SDCC（ABL 交过来的配置下）压根没广告 ADMA2。** MSM 上的 ADMA 需要额外的
vendor 侧配置（`CORE_VENDOR_SPEC` 里的 DMA 位 + 描述符表的物理约束），SDMA 才是
开箱可用的那条路。v20~v22 一直在 ADMA2 上撞，所以 `eis=0x200` 从来不变。

### 3.70.1 v23 改动

`beginDmaRead()` 先读 `CAPABILITIES` 做路由：

* `bit27` 有 → ADMA2（保留 v22 的 32/64 位两种条目布局）；
* `bit27` 无、`bit26` 有 → **SDMA**：
  * 把 `Host Control 1` 的 `DMA Select` 拨回 `0`；
  * 直接把**数据缓冲区的物理地址**写进 `0x58`（SDMA 没有描述符表，这个寄存器
    在 SDMA 模式下就是系统内存地址）；
  * 日志 `dma: sdma armed layout=sdma caps=… bytes=… addr=… hostctl=…`。
* `dma: armed` 顺带打印 `caps`，方便对照。

### 3.70.2 判据

| 日志 | 结论 |
|---|---|
| `dma: sdma armed … hostctl=00` + `dma: done ok=1` + 非 0 数据 | SDMA 通了 |
| `dma: sdma armed` 但 `data timeout` 且 `adma` 没变 | SDMA 也没启动，回头查 `Host Control 1` 是否真被写进去 |
| `dma: done ok=1` 但 `dma: data` 全 0 | 缓存/一致性：`prepare/complete` 没生效，需要 IODMACommand |
| 出现 `EXT_CSD rev=… SEC_COUNT=…` | 64 GB 容量第一次被内核读出来 |

顺便记一条经验：**先读 CAPABILITIES 判断硬件到底支持什么，再写驱动**。
前几轮一直在"按 sdhci 通用流程实现 ADMA2"，却没验证这块控制器广告了 ADMA2 没有，
白烧了三轮（v20/v21/v22）。

## 3.71 SDMA 也不出数据 → 打开中断使能 + 状态演化探针（v24）

v23 日志（SDMA 路径）：

```
dma: sdma armed layout=sdma caps=742dc8b2 bytes=512 addr=aff40000 hostctl=00
cmd8 pre state=01f000f0 adma=aff40000
cmd8 posted state=01f000f1
cmd8 complete state=01f002f6          <- 这次没有 eis=0x200，ADMA 错误消失
CMD8 ok state=01f002f6 nis=00000000 eis=00000000 adma=ffffffff
data timeout nis=0000 eis=0000 state=01f002f6
dma: done ok=0 … adma=ffffffff err=00000000/00000000
dma: data 00000000 …
```

三条新信息：

1. **切到 SDMA 后 ADMA Error 消失了**（命令阶段干净），说明 DMA 模式选择是对的；
2. `0x58` 在 SDMA 模式下读回 `ffffffff`（该寄存器在 SDMA 模式下没有回读意义，正常）；
3. 数据引擎完全不动：`NIS` 里既没有 `BUFFER_READ_READY(bit5)` 也没有 `DATA_END(bit1)`，
   `DAT_INHIBIT(bit1)=1`、`DAT_LINE_ACTIVE(bit2)=1` 从头到尾不变。

把 PIO(v19)、ADMA2(v20~22)、SDMA(v23) 三条路径对齐看，失败模式是一致的：
**控制器认为"传输已经开始"，但 DAT 线上没有任何东西进来**。

### 3.71.1 发现一个一直被忽略的寄存器

翻遍驱动，我们**从来没有写过 SDHCI 的中断使能寄存器**：

* `SDHCI_NORMAL_INT_ENABLE (0x34)`
* `SDHCI_ERROR_INT_ENABLE (0x36)`
* `SDHCI_NORMAL_INT_SIGNAL (0x38)` / `SDHCI_ERROR_INT_SIGNAL (0x3A)`

Linux 的 `sdhci_enable()` 第一步就是 `sdhci_writew(SDHCI_INT_ENABLE, …)` +
`sdhci_writew(SDHCI_SIGNAL_ENABLE, …)`。部分控制器（含 MSM 系列）的数据引擎
推进与"状态位使能"相关——状态位不使能时，内部状态机可能不前进。这条线索之前
完全没有验证过。

### 3.71.2 v24 改动

1. `start()` 在 `clock done` 之后：

   ```c
   sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, 0xFFFF);
   sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS,  0xFFFF);
   sdhciWrite16 (fCore, SDHCI_NORMAL_INT_ENABLE, 0xFFFF);
   sdhciWrite16 (fCore, SDHCI_ERROR_INT_ENABLE,  0xFFFF);
   sdhciWrite16 (fCore, SDHCI_NORMAL_INT_SIGNAL, 0x0000);
   sdhciWrite16 (fCore, SDHCI_ERROR_INT_SIGNAL,  0x0000);
   ```

   状态位全使能、信号全关（我们纯轮询，不需要中断线），并回读打印
   `int: enable=%04x/%04x signal=%04x/%04x`。

2. `readExtCsd()` 在命令完成后连采 8 次状态（间隔 5 ms）：

   ```
   trace <n> state=%08x nis=%08x
   ```

   看 `DAT_LINE_ACTIVE` / `DOING_READ` / `DATA_AVAILABLE` 有没有变化。

3. `endDmaRead()` 失败时补一行上下文：

   ```
   dma: ctx blksz=… cnt=… mode=… hostctl=… vs0=… vs3=… mstat=…
   ```

### 3.71.3 判据

| 日志 | 结论 |
|---|---|
| `trace` 里 `state` 从 `01f002f6` 变成 `01f000f6`/`…`，`nis` 出现 `0001`(DATA_END) | 中断使能就是那个开关，直接通 |
| `trace` 全程一模一样 | 数据引擎确实没推进，下一步查 DAT 线/时钟/卡侧模式 |
| 出现 `EXT_CSD rev=… SEC_COUNT=…` | 64 GB 容量读出来了 |

## 3.72 NativeSdhci v25：从 DMA 退回 PIO，不再猜收尾位，直接看 FIFO 里到底有没有东西（2026-10-27）

### 3.72.1 为什么又退回 PIO

v20 → v22 三轮都在 ADMA2 上撞墙，最后靠读 CAPABILITIES 才明白方向错了：

```
caps=742dc8b2 = 0111 0100 0010 1101 1100 1000 1011 0010
bit28=1  64 位系统总线
bit27=0  ADMA2 不支持      <- v20/v21/v22 一直在撞这条
bit26=1  SDMA 支持
```

v23 改成 SDMA 之后 `eis=0x200`（ADMA Error）确实消失了，按说模式选对了，
但 v23/v24 的数据引擎仍然彻底冻结：

```
v23:  cmd8 complete state=01f002f6 → CMD8 ok state=01f002f6 nis=00000000 eis=00000000 adma=ffffffff
      data timeout nis=0000 eis=0000 state=01f002f6
v24:  trace 0..7 state=01f002f6 nis=00000000   （8 次采样一模一样）
      dma: ctx blksz=0200 cnt=0000 mode=0013 hostctl=0000ff00 vs0=000010a0e vs3=02626440 mstat=0007f880
```

`NIS` 里既没有 `DATA_END(bit1)` 也没有 `BUFFER_READ_READY(bit5)`，中断使能打开后
也毫无变化（v24 第一次真正写了 `NORMAL/ERROR_INT_ENABLE`）。

回头看，唯一"有反应"的一次其实是 **v19 的 PIO**：

```
pio got 512 words (block=128) first=00000000 00000000 00000000 00000000 last=…
pio after state=01f00af6 nis=00000020
```

它拿到了 `BUFFER_READ_READY`，而且读满 128 个字之后 `BUFFER_READ_ENABLE(bit11)` 仍然是 1。
当时把 `first` 全是 0 当成"FIFO 给空数据"，但 **EXT_CSD 的前 16 字节本来就是 0x00**——
所以那个 0 什么都证明不了。真正能证明"数据到了"的是：

| word | EXT_CSD 字节 | 内容 |
|---|---|---|
| `w0`/`w1` | [0..7] | 固定 0（无意义） |
| `w52` | [208..211] | `EXT_CSD_REV` 附近 |
| `w53` | [212..215] | **`SEC_COUNT` 低 32 位（容量）** |
| `w126`/`w127` | [504..511] | 保留区 |

### 3.72.2 v25 改动

1. `readExtCsd()` 与 `readBlocks()` 的 `TRANSFER_MODE` 去掉 `SDHCI_TRNS_DMA`，
   回到 `SDHCI_TRNS_READ | SDHCI_TRNS_BLK_CNT_EN`（两处）。
2. 不再走 `beginDmaRead()/endDmaRead()`，改用 `pioTransfer(true, …)` 把 FIFO drain 掉。
3. 无论收尾位如何，都把关键字打出来：
   - EXT_CSD：`ext w0=%08x w1=%08x w52=%08x w53=%08x w126=%08x w127=%08x drained=%u`
   - 数据块：`lba%llu w0=%08x w1=%08x w126=%08x w127=%08x drained=%u`
4. 判定放宽：`drained || w53 != 0`（EXT_CSD）/ `drained || w0 != 0 || w127 != 0`（数据块）
   即认为成功 —— 先把"能不能读到内容"和"收尾位对不对"这两件事拆开。
5. `readBlocks()` 里 dump 用 `sdhciCopyLe()` 逐字节拼，不再把 `void *buffer`
   强转成 `uint32_t *`（调用方不保证 4 字节对齐）。
6. `kSdhciTag = "v25"`。

### 3.72.3 下一刷判读表

| 现象 | 结论 | 下一步 |
|---|---|---|
| `w53=0000xxxx`（非 0） | 数据一直在来，只是收尾判定不对 | 把收尾条件改成"FIFO 空且无新数据"，一路读到 GPT |
| `drained=1` 但 `w53=0`、`w126/w127=0` | 卡确实没驱动 DAT 线 | 查 `vs0`/`mstat`、试 CMD13 读卡侧 `CURRENT_STATE`、降速到 400 kHz 重试 |
| `pio read starved at word 0` | 一个字节都没到 FIFO | 回到 DMA 路线，查 `CORE_DLL_CONFIG(0x200)` 采样点/`CORE_DDR_CONFIG(0x25c)` |
| 出现 `EXT_CSD rev=… SEC_COUNT=…` | PIO 打通 | 立刻接 `readBlocks(0)` 看 MBR/GPT |

## 3.73 NativeSdhci v26：数据一个字节都不来 —— 先把卡的 CURRENT_STATE 问出来（2026-10-27）

### 3.73.1 v25 真机结果：控制器开着数据引擎，等待卡发数据，卡没有发

```
CMD8 setup blksz=0200 cnt=0001 mode=0012 word28=0000ff18 attempt=0
cmd8 pre state=01f800f0 adma=00000000
cmd8 posted state=01f800f1
cmd8 complete state=01f80af6
trace 0..7 state=01f80af6 nis=00000000
CMD8 ok state=01f80af6 nis=00000000 eis=00000000 adma=00000000
pio got 512 words (block=128) first=00000000 00000000 00000000 00000000 last=00000000
pio after state=01f80af6 nis=00000000 eis=00000000
data timeout nis=0000 eis=0000 state=01f80af6
ext w0=00000000 w1=00000000 w52=00000000 w53=00000000 w126=00000000 w127=00000000 drained=0
```

把 `state=01f80af6` 拆开：

| 位 | 名字 | 值 |
|---|---|---|
| bit1 | `DATA_INHIBIT` | 1（控制器认为数据阶段没结束） |
| bit2 | `DAT_LINE_ACTIVE` | 1 |
| bit9 | `READ_TRANSFER_ACTIVE` | 1 |
| bit11 | `BUFFER_READ_ENABLE` | 1 |
| bits20..23 | `DAT[3:0]` 电平 | `1111` = 四条数据线全是空闲高电平 |
| bit24 | `CMD_LINE_LEVEL` | 1 |

也就是说：**控制器把数据引擎打开了、DAT 线一直空闲、卡一个字节都没发**；
而命令阶段是干净的（`cmd8 complete`、`nis=0`、`eis=0`），说明卡 ACK 了 CMD8。

这排除了"FIFO 读法错"和"收尾位错"两种可能 —— 数据根本没到。PIO 读 512 次
（`pio got 512 words`）全部读到 0，也印证 FIFO 里是空的，控制器只是按
`BUFFER_READ_READY` 反复给 0。

### 3.73.2 唯一还没看过的东西：R1

一路以来 `waitCommandComplete()` 只关心"命令完成没完成"，**从来没看 R1 的内容**。
eMMC 在 R1 里报告 `CARD_IS_LOCKED`、`ILLEGAL_COMMAND`、`ADDRESS_OUT_OF_RANGE`、
`CURRENT_STATE` 等标志；如果卡认为自己不处于 `tran` 状态，它 ACK 命令之后
**按规范不会发数据** —— 完全对应上面的现象。

### 3.73.3 v26 改动（纯观测）

1. `CMD8` / `CMD17` 的 `sendCommand()` 传入 `r1[4]`，命令完成后打印
   `CMD8 r1=%08x current_state=%u (%s)`。
2. 新增 `bool NativeSdhci::logCardStatus (const char *who)`：发 **CMD13（SEND_STATUS，
   arg = RCA<<16，R1）**，打印 `status(%s) r1=%08x current_state=%u (%s) ready=%u`。
3. 调用点：`CMD16` 之后（初始化完成、进入数据阶段之前）一次；`CMD8`、`CMD17`
   每次数据阶段失败后各一次。
4. 新增 `static const char * sdhciCardStateName (uint32_t)`：0 idle / 1 ready /
   2 ident / 3 stby / 4 tran / 5 data / 6 rcv / 7 prg。
5. `kSdhciTag = "v26"`。

### 3.73.4 下一刷判读表

| 屏幕上的行 | 结论 | 下一步 |
|---|---|---|
| `status(after CMD16) … current_state=4 (tran)` 且 `CMD8 r1=00000900`(bit8 READY_FOR_DATA) | 卡状态正常，问题在数据线/采样 | 查 `CORE_DLL_CONFIG(0x200)` 采样点、`CORE_DDR_CONFIG`，或把 SDCLK 降到 100 kHz 试 |
| `current_state` 不是 4 | 卡没进入传输态 | 按状态补命令（如 `stby` 就重发 CMD7；`data`/`rcv` 先 CMD12 abort） |
| `CMD8 r1` 的 bit5(`ILLEGAL_COMMAND`) 置位 | 卡不认 CMD8，或 RCA 不对 | 重做 CMD3（发新 RCA）/检查 `fRca` |
| `CMD8 r1` 的 bit25(`CARD_IS_LOCKED`) 置位 | 卡被锁 | 需要先解密码，这条基本走死 |
| `status(...) CMD13 failed` | 连 CMD13 都发不出去 | 回到命令线/时钟问题 |

## 3.74 NativeSdhci v27：卡说"我在 tran、准备好"，数据却不在总线上 → 存证 ABL 的寄存器状态（2026-10-27）

### 3.74.1 v26 真机结果：R1 干净，数据线全空闲

```
cmd8 complete state=01f80af6
CMD8 r1=00000900 current_state=4 (tran)
trace 0..7 state=01f80af6 nis=00000000
CMD8 ok state=01f80af6 nis=00000000 eis=00000000 adma=00000000
pio got 512 words (block=128) first=00000000 00000000 00000000 00000000 last=00000000
data timeout nis=0000 eis=0000 state=01f80af6
ext w0=00000000 w1=00000000 w52=00000000 w53=00000000 w126=00000000 w127=00000000 drained=0
EXT_CSD transfer failed (attempt 0)
status(after CMD8 data failure) r1=00000900 current_state=4 (tran) ready=1
CMD17 r1=00000900 current_state=4 (tran)
```

`R1 = 0x00000900`：

| 位 | 含义 | 值 |
|---|---|---|
| bit8 | `READY_FOR_DATA` | 1 |
| bits12..9 | `CURRENT_STATE` | 4 = tran |
| bit5 | `ILLEGAL_COMMAND` | 0 |
| bit25 | `CARD_IS_LOCKED` | 0 |

卡自认为处于传输态、准备好了、没有任何错误。控制器一侧同样"正常"：
`DATA_INHIBIT`/`DAT_LINE_ACTIVE`/`READ_TRANSFER_ACTIVE`/`BUFFER_READ_ENABLE` 全在，
而 `DAT[3:0]` 电平（state bits20..23）= `1111`，四条数据线一直空闲高电平。

**结论：命令通道完全正常（CMD 线上能收到 R1），数据通道上一个字节都没出现。**
这已经不是"我们读 FIFO 的方式不对"或"收尾位判断不对"，而是数据根本没上总线。

### 3.74.2 为什么去 dump 寄存器

我们从来没看过 ABL 交接那一瞬间 SDCC 的完整寄存器状态。而 ABL(LK) 是能正常
读这块 eMMC 的 —— 它自己在 HS400 上跑、把 Android 从这块盘上加载起来。
也就是说：**ABL 留下的那份寄存器配置是"能工作"的**，而我们在
`configureVendorSpec()` / `setBusPowerAndClock()` 里改过 MCLK_SEL、
HC_SELECT_IN、IO pad、软件复位、时钟、中断使能等等。

最省事的判据：把两份 dump 放在一起对比，差的那几位就是数据路径缺的东西。

### 3.74.3 v27 改动

1. 新增 `void NativeSdhci::dumpRegs (const char *tag)`：
   - `core 0x000 .. 0x0FC`，每行 4 个 32 位字；
   - `vendor 0x200 .. 0x330`，每行 4 个 32 位字；
   - 全部偏移都是 4 的倍数 —— **绝不再踩错位 32 位读**（`docs §3.50` 那次整机静默就是这个坑）。
2. 调用两次：
   - `start()` 里 **GCC 时钟打开之后、第一次写 SDCC 之前** → `dump abl`（= ABL 交接状态）；
   - `readExtCsd()` 数据阶段失败之后 → `dump after`（= 我们初始化之后的状态）。
3. `kSdhciTag = "v27"`。

注意 dump 位置特意放在 `enableGccClocks()` 之后：时钟没开时读 SDCC 在 MSM 上会把
AHB 挂死，连 log 都打不出来。

### 3.74.4 下一刷要盯的寄存器

| 偏移 | 名字 | 关注点 |
|---|---|---|
| `0x200` | `CORE_DLL_CONFIG` | ABL 在 HS400 会打开 DLL（`CORE_DLL_EN`/`CDR_EN`/`CK_OUT_EN`）；若 `after` 里这些位还在而我们跑 400 kHz，可能反而坏事 |
| `0x208` | `CORE_DLL_STATUS` | `CORE_DLL_LOCK`(bit7) |
| `0x20c` | `CORE_VENDOR_SPEC` | `MCLK_SEL`(bits9:8)、`HC_SELECT_IN`(bits21:18)、`IO_PAD_PWR_SWITCH_EN`(15)/`SWITCH`(16) |
| `0x250` | `CORE_VENDOR_SPEC3` | `CORE_PWRSAVE_DLL`(bit3) |
| `0x25c` | `CORE_DDR_CONFIG` | DDR / 采样相关 |
| `0x28` | `HOST_CONTROL`（core 段） | DMA Select / 位宽 |
| `0x2C` | `CLOCK_CONTROL` | `INT_CLK_STABLE`(bit1) 是否真的置起来了 |
| `0x324` | `CORE_MCI_STATUS` | MSM 内部状态位 |

判读思路：如果 `abl` 里某个"数据路径开关"在 `after` 里被我们关掉/改掉了，
下一版就把它恢复；如果两份几乎一样，说明问题不在寄存器配置，而在
TLMM 引脚复用（DAT 线可能没接到 SDC）或外部电平转换器 —— 那时再 map
TLMM（`pinctrl@03000000`）逐个 pin 打印。

## 3.75 NativeSdhci v28：after dump 读出两个硬伤（总线电源被关、IO pad EN 没置）

v27 的 `after` dump 一屏把数据阶段的现场拍下来了，直接把问题缩到两个寄存器上。

### 3.75.1 after dump 的读数（4 个 32 位字一行）

```
after 020: 00000000 01f80af6 00000010 000e0007
           0x20     0x24     0x28     0x2C
after 200: 0785642c 00000000 0000001c 00010a0e
           0x200    0x204    0x208    0x20C
after 240: 00000000 00000000 00000000 00000001
           0x240    0x244    0x248    0x24C
after 250: 02626440 00008000 00000000 00004073
           0x250    0x254    0x258    0x25C
after 318: 1000006b 00000000 00f04003 0007f800
           0x318    0x31C    0x320    0x324
```

| 寄存器 | 值 | 含义 |
|---|---|---|
| `HOST_CONTROL(0x28)` | `0x10` | DMA select = 2（ADMA2-32），低字节没毛病 |
| `POWER_CONTROL(0x29)` | `0x00` | **总线电源是关的** |
| `CLOCK_CONTROL(0x2C)` | `0x000e0007` | `INT_CLK_STABLE=1`、`CARD_CLK_EN=1`、TIMEOUT=0x0e、复位字节=0 → 时钟本身是好的 |
| `VENDOR_SPEC(0x20C)` | `0x00010a0e` | `MCLK_SEL=2`✓、`IO_PAD_PWR_SWITCH(bit16)=1`，但 **`IO_PAD_PWR_SWITCH_EN(bit15)=0`** |
| `CORE_PWRCTL_CTL(0x24C)` | `0x00000001` | `BUS_OFF` |
| `DLL_CONFIG(0x200)` / `DLL_STATUS(0x208)` | `0x0785642c` / `0x1c` | `DLL_EN=1`，但没有 lock 位 |
| `DDR_CONFIG(0x25C)` | `0x00004073` | 还是 ABL 留下的 DDR 配置 |
| `MCI_VERSION(0x318)` | `0x1000006b` | core 1.6b（与启动日志一致） |
| `MCI_STATUS(0x324)` | `0x0007f800` | 无错误位 |

### 3.75.2 两个硬伤

1. **`POWER_CONTROL = 0`。** `setBusPowerAndClock()` 里写过
   `SDHCI_POWER_ON | SDHCI_POWER_330`（那一版的 `word28=0000ff18`，低字节 `0x18`、
   次字节 `0x0f` = `POWER_330|POWER_ON`）。但随后 `recoverCommandState()` 的
   `RESET_CMD|RESET_DATA` 与时钟设置路径把它又清回了 0，之后没人再置位。
2. **`IO_PAD_PWR_SWITCH_EN(bit15)` 一直是 0。** Linux `sdhci_msm_set_vmmc()`
   （`sdhci-msm.c:2160-2177`）明确**先置 EN 位**，再按电压置/清 `SWITCH(bit16)`；
   只有 `SWITCH` 没有 `EN`，1.8V 选择根本不生效 —— 而 R11 的 VCCQ 就是 1.8V。

这两条都能解释"命令通道全通（CMD1/CMD2/CMD3/CMD7/CMD13 的 R1 干净）、
但数据线一个字节都不动（`DAT[3:0]` 恒为 1111）"。

### 3.75.3 v28 的两处改动

* `dumpRegs()` 改成 **快照 + diff**：`kSdhciDumpOffsets[]` 43 个对齐偏移，
  第一次调用（`abl`）只存快照打 `===== snapshot abl: 43 regs held =====`；
  之后只打印"变了的" `%03x: %08x -> %08x` + `changed=%u/%u`，再补三行固定关注项
  （`host28/clk2c/state24`、`dll200/dllst208/vs20c`、`pwr240/pwr24c/ddr25c/mstat324`）。
  一屏看完，拍照不用拼图。
* 新增 `ensureBusState(const char *why)`：每次发数据命令（CMD8 / CMD17）之前，
  1) 把 `0x28` 字的次字节重写为 `SDHCI_POWER_ON|SDHCI_POWER_330`、低字节保持 1-bit
  （清 `8BITBUS|4BITBUS`）；
  2) 把 `0x20C` 的 `bit15|bit16` 一起置上。
  打 `bus[cmd8] host28=… -> … vs0=… -> …`。

### 3.75.4 下一刷判读表

| 现象 | 结论 |
|---|---|
| `bus[cmd8] host28=00000010 -> 00000f10` | 电源位置上了（`0x0f` = `POWER_330|POWER_ON`） |
| `vs0=00010a0e -> 00018a0e` | IO pad EN 置上了（bit15=1） |
| `diff after` 里 `028: 00000f10 -> 00000010` | 电源位又被复位吃掉了 → 要在复位之后、每条命令之前都补 |
| `pio got 512 words` 里 `first` 非 0 | **数据通了**，接着看 `w52/w53`（`SEC_COUNT`） |
| 仍然全 0，且 `diff` 显示 `20c` 没变 | 写 `IO_PAD_PWR_SWITCH` 被硬件忽略 → 转 TLMM（`0x03000000`）查 DAT 引脚配置 |
| 仍然全 0，`diff` 里 `2c` 掉到 `000e0003` | `CARD_CLK_EN` 被清 → 时钟在命令后掉了，需在命令前重开 |

## 3.76 NativeSdhci v29：电源与 IO pad 都补上了，数据仍不来 → 按 SDR 模式重配数据通路

### 3.76.1 v28 真机结果（关键读数）

```
after key host28=0000ff18 clk2c=000e0007 state24=01f80af6
after key dll200=0705642c dllst208=0000001c vs20c=00018a0e
after key pwr240=00000000 pwr24c=00000001 ddr25c=00004073 mstat324=0007f800
bus[cmd8] host28=0000ff18 -> 0000ff18 vs0=00018a0e -> 00018a0e
diff after (only changed regs)
after 000: 00020000 -> 00010000
after 00c: 0d020003 -> 0d1a0012
after 010: 00000000 -> 00000900
after 024: 01f800f0 -> 01f80af6
after 028: 00006030 -> 0000ff10
after 02c: 000f0007 -> 000e0007
after 034: 07ff003f -> fffff7ff
after 03c: 00030000 -> 00000000
after 20c: 00240a0e -> 00018a0e
after 304: 00000000 -> 0000000c
after 308: 00000000 -> 00000200
after 324: 00007800 -> 0007f800
after changed=12/43
```

| 结论 | 依据 |
|---|---|
| IO pad EN 补上了 | `vs20c` 从 `0x00010a0e` 变成 `0x00018a0e`（bit15 \| bit16 都在） |
| 总线电源补上了 | `host28` 的次字节不再是 0（`0000ff18`） |
| 时钟一直是好的 | `clk2c=000e0007`：`INT_CLK_STABLE=1`、`CARD_CLK_EN=1` |
| 命令通道一直是好的 | `CMD8 r1=00000900 current_state=4 (tran)` |
| **数据仍然 0 字节** | `pio got 512 words … first=00000000`、`data timeout nis=0000 eis=0000` |
| DDR 配置还是 ABL 的 HS400 值 | `ddr25c=0x00004073` |

### 3.76.2 转向"数据接收路径"

命令通道通、数据通道死，说明问题在 MSM 内部**采数路径**（不是引脚、不是时钟、不是供电）。
Linux `sdhci-msm.c` 对默认速（非 tuning）模式做三件我们没做的事：

1. `sdhci_msm_set_cdr(host, false)`（:1165-1184）：
   `config &= ~CORE_CDR_EN (bit17)`，`config |= CORE_CDR_EXT_EN (bit19)`。
2. `CORE_DDR_CONFIG(0x25C)`：Linux 注释（:1016-1027）明确"该寄存器 POR 默认值就是
   SDR 要的配置，bootloader 改过就要写回去"。我们现在拿 ABL 的 HS400 值（`0x4073`）跑 SDR。
3. `CORE_CLK_PWRSAVE(bit1, VENDOR_SPEC)`：`vs0=0x00018a0e` 里 bit1=1，空闲时允许门控 MCLK；
   数据阶段需要 MCLK 连续。

### 3.76.3 v29 改动

全塞进 `ensureBusState()`（每次 CMD8/CMD17 前都会跑），三处写都带 before→after：

* `0x200 DLL_CONFIG`：清 bit17、置 bit19；
* `0x25C DDR_CONFIG`：写 0；
* `0x20C VENDOR_SPEC`：清 bit1（`CORE_CLK_PWRSAVE`）。

新增日志：
```
path[cmd8] dll=0705642c -> … ddr=00004073 -> … vs0=… -> …
```

### 3.76.4 下一刷判读表

| 现象 | 结论 |
|---|---|
| `path[cmd8] dll` 的 bit19 置上了、`ddr` 变 00000000 | 三处写都被硬件接受 |
| `dll` 写不进去（读回原值） | 需要先置 `CORE_DLL_PDN/RST` 再改 → 补 DLL 复位序列 |
| `ddr` 写不进去 | `0x25C` 在这个 core 版本是只读的，改走 `core_ddr_config_old`(0x1b8) |
| `pio got` 里出现非 0 字 | **数据通了**，接着看 `w52/w53`（`SEC_COUNT`） |
| 仍然全 0 | 采数路径还是错，下一步把 `CORE_HC_SELECT_IN` 显式设成 1(2^0) / 2 / 4 逐个试，并读 DLL_STATUS 的 lock 位 |


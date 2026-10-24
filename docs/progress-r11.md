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

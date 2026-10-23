# 在 Windows 上构建（WSL2 路线）

结论先说：**Windows + WSL2 能做到 bring-up 阶段 2；阶段 3 起必须 macOS。** 依据是代码里的硬检查：

| 代码 | 事实 |
|---|---|
| `scripts/unixnu/cli.py:207` | 非 Darwin 只打印警告，**不退出** → CLI 本身跨平台 |
| `kernel.py:75` | `tool('xcodebuild')` → 只有**内核步骤**锁 macOS（iphoneos SDK） |
| `payload.py:57` | 只要 `mformat`/`mcopy`；`--skip-setup` 才用 `hdiutil` |
| `userdata.py:58` | `hdiutil` 挂载 dmg → **首次安装镜像**那步要 macOS |

所以 `build firmware`（EDK2）和 `bootimg --ramdisk none`（BootShim + mkbootimg + mtools）**不需要 Xcode**。

| 阶段 | Windows+WSL2 | macOS |
|---|---|---|
| 1 UEFI 出画面/菜单 | ✅ | ✅ |
| 2 UniXNU 菜单（空 ramdisk） | ✅ | ✅ |
| 3 payload 的 `xnu` 项（要 `kernel.macho`） | ❌ | ✅ |
| 5 内核打印 / 6 根卷 / 7 用户态 | ❌ | ✅ |

---

## 1. 装 WSL2 到 E 盘（管理员 PowerShell，需要重启）

C: 只有 18.4 GB 可用，而 WSL 的 `ext4.vhdx` 默认落在 C: 且**只增不减**。装到 E:（135 GB 可用）：

```powershell
wsl --version          # 2.4.4+ 支持 --location
wsl --install -d Ubuntu-24.04 --location E:\WSL\Ubuntu-24.04
```

版本旧就先默认装、再迁移：

```powershell
wsl --shutdown
wsl --export Ubuntu-24.04 E:\WSL\ubuntu.tar
wsl --unregister Ubuntu-24.04
wsl --import Ubuntu-24.04 E:\WSL\Ubuntu-24.04 E:\WSL\ubuntu.tar --version 2
```

`--import` 之后默认用户变成 root，进系统建用户并写 `/etc/wsl.conf`：

```ini
[user]
default=你的用户名
```

> 用 **24.04**，不要 22.04：`cli.py` 要求 Python ≥ 3.11，22.04 自带的是 3.10，还得加 PPA。

重启后首次进入 Ubuntu，建好用户名密码。

## 2. 装依赖（一条命令）

```bash
cd /mnt/e/DeepSeekWorkspace/unixnu-r11/scripts
dos2unix wsl-setup.sh build-stage1.sh 2>/dev/null || sed -i 's/\r$//' wsl-setup.sh build-stage1.sh
chmod +x wsl-setup.sh build-stage1.sh
./wsl-setup.sh
```

它做三件事：`apt` 装齐 `clang/lld/llvm/nasm/make/cmake/ninja/mtools/dtc/acpica-tools/...`、
校验 Python ≥ 3.11、逐个检查工具是否在 `PATH`。

## 3. 目录策略（能省一半时间）

WSL2 访问 `/mnt/e` 是 9p 协议，**编译 edk2 会慢 5–10 倍**。做法：

```bash
mkdir -p ~/src
cp -r /mnt/e/DeepSeekWorkspace/src/Mu-Silicium   ~/src/
cp -r /mnt/e/DeepSeekWorkspace/src/UniXNU        ~/src/
cp -r /mnt/e/DeepSeekWorkspace/uefi-r11          ~/src/
cp -r /mnt/e/DeepSeekWorkspace/unixnu-r11        ~/src/
```

之后所有构建都在 `~/src` 里做，产物再拷回 `/mnt/e`（给 Windows 侧的 fastboot 用）。

## 4. 合并平台包进 Mu-Silicium 并构建

`uefi-r11` 是**待合并的增量**：`Platforms/OPPO/r11Pkg`、`Resources/Configs/r11.conf`、`Binaries/r11`。
脚本会自动拷贝、清掉 CRLF、跑构建、检查固件卷大小：

```bash
cd ~/src/unixnu-r11
MU=~/src/Mu-Silicium UE=~/src/uefi-r11 ./scripts/build-stage1.sh
```

判定：

- `SILICIUM_UEFI.fd` 存在且 **≤ 0x300000（3 MiB）**
- `out/r11/boot/boot.img` 存在（这一步用 `--ramdisk none`，只是空负载冒烟）

## 5. ⚠️ WSL2 里没有 USB，刷机必须在 Windows 侧

WSL2 默认**不通 USB**（要装 usbipd-win 才能直通）。所以：

```powershell
# Windows 侧
adb devices                                   # 或 adb reboot bootloader; fastboot devices
fastboot boot E:\DeepSeekWorkspace\out\r11\boot\boot.img
```

把 WSL 里的 `out/r11/boot/boot.img` 拷到 `E:\DeepSeekWorkspace\out\r11\boot\` 再执行。

`fastboot boot` 是**内存启动**：上限是 `max-download`（R11 是 512 MiB），**不受 boot 分区 64 MiB 限制**；
只有之后 `fastboot flash boot` 才必须 ≤ 64 MiB。

失败就 `fastboot reboot`，Flyme 原样回来。

## 6. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `unixnu: needs Python 3.11 or newer` | 装的是 22.04 → 换 24.04，或 `sudo add-apt-repository ppa:deadsnakes/ppa` |
| `tool clang not found` | 只装了 `llvm` 没装 `clang`；`apt install clang lld` |
| `GenFw: command not found` 之类 | EDK2 BaseTools 没编出来；先 `make -C $MU/BaseTools` 或让驱动自动建（首次会慢） |
| `mformat: command not found` | 缺 `mtools`（payload 卷要用） |
| `Syntax error: "\r"` / 奇怪的解析错误 | 文件带了 CRLF；`build-stage1.sh` 里有清理步骤，若手工合并记得 `sed -i 's/\r$//'` |
| `iasl: not found` | 缺 `acpica-tools`（Silicium-ACPI 要用） |
| 固件卷 > 0x300000 | 组件塞太多；砍掉不用的 INF（`Include/DXE.inc`）或调 `r11.conf` 的 `TARGET_FD_SIZE` |
| `fastboot boot` 卡住不复位 | 说明 BootShim 跳进去了但 FD 没起来 → 看屏幕；FD 基址或 `[boot_image]` 参数不对 |
| 屏幕一直显示 OPPO logo | 面板是 ABL 保持点亮的（cont-splash），**logo 不代表 UEFI 跑起来**；必须看到菜单文字 |

## 7. 到阶段 3 时怎么办

`payload` 里的 `xnu` 菜单项需要 `kernel.macho`（预链接内核），它由 `unixnu build kernel` 产出，
而该步骤强制 `xcodebuild` + `iphoneos` SDK。此时只剩两条路：

1. 真 Mac / 黑苹果 / macOS 云主机；
2. GitHub Actions 的 `macos-*` runner（主仓自带 `.github/workflows`），产物用 artifact 取回。

WSL 阶段的所有输入（`board.toml`、`r11_defconfig`、`adt/`、`drivers/`、`Binaries/r11`）都是同一份，
换到 macOS 上不需要改任何东西。

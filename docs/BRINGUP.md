# R11 bring-up 执行清单

每个阶段只允许动**一个**变量；每阶段结束把日志 + 照片存进 `docs/logs/<stage>/`。
打 ⛔ 的是“没过就别往下做”的闸门。

---

## S0 · 备份与取证（不改机）

- [ ] `fastboot getvar all` → BL 状态、slot、分区表存 `docs/logs/s0/`
- [ ] `adb shell 'cat /proc/partitions'`、`dmesg` 全文
- [ ] stock 内核树 clone → `scripts/probe-hw.ps1` 跑通，硬件表无 ❓
- [ ] EDL：`edl printgpt` + 全量 readback，`full_backup.bin` 存两份（异地一份）
- [ ] **回刷演练**：把备份的 `boot` 刷回去，确认能开机
- [ ] ⛔ EDL 不能写 / 没有 programmer → 停

## S1 · UEFI 能起、能出画面

- [ ] `uefi-r11` 的 `R11Pkg.dsc`/`.fdf` 能编译出 FD
- [ ] FD 刷进 `boot`，上电后 FrameBuffer 有早期日志（哪怕只有一行）
- [ ] panel init 出图（先 1080×1920 linear，再 GOP 1024×768 letterbox）
- [ ] ⛔ 屏幕不出图 → 不要往下做，先解决 DSI

## S2 · 菜单 + 存储

- [ ] Boot Manager 菜单可见
- [ ] `BlockIo` 能读 microSD（FAT32）
- [ ] UEFI Shell 能 `ls`
- [ ] ⛔ 读不到 SD → 查 SDHCI 时钟/电压（3.3V vs 1.8V）

## S3 · payload

- [ ] loader 读 ESP 里的 `mach_kernel` + `r11.adt` + `boot.cfg`
- [ ] 打印每段大小与 CRC，全绿
- [ ] `ExitBootServices` 后能跳进 XNU 的 `_start`

## S4 · 内核入口

- [ ] 第一行 `Darwin Kernel Version ...` 出现（先单核：`cpus=1`）
- [ ] 页大小 16K 生效（`debug=0x144` 里的 pmap 打印）
- [ ] ⛔ 一进来就 triple fault → 查 entry 地址 / 页表 / 栈

## S5 · Platform Expert

- [ ] `IODeviceTree` 列出 `/cpus`、`/arm-io`、`/product`
- [ ] `AppleARMPE` 注册成功，`PE_init_platform` 走完
- [ ] 中断能收到（timer tick 打印）
- [ ] ⛔ ADT 少节点 → 回 `adt/r11.dts` 补，别改内核

## S6 · 存储

- [ ] SDHCI polling 模式认到卡
- [ ] 切中断模式（GIC 通了）
- [ ] `IOMedia` 出现，能读第一个 block

## S7 · 根卷

- [ ] HFS+ 根卷挂上（microSD 优先）
- [ ] `-s` 单用户 shell 拿到 root
- [ ] ⛔ APFS 现在别碰

## S8 · 用户区

- [ ] `launchd` 起来（先 `-x` 安全模式，减 kext）
- [ ] 进程树 > 10 个
- [ ] AMFI/SEP 相关 kext 全部 stub 掉后不再 panic
- [ ] 有 `WindowServer`（软件渲染，慢没关系）

---

## 常见死因速查

| 现象 | 第一怀疑 | 命令/手段 |
|---|---|---|
| UEFI 无任何输出 | FD 基址错 / GCC 时钟未开 | 换 `PcdFdBaseAddress`；在 `PlatformLib` 里先硬开 UART+SDCC 时钟 |
| 出图但花屏 | DSI PHY 参数 / panel 时序 | 抄 stock dtsi 的 `qcom,mdss-dsi-timing` 一字不改 |
| 触摸无反应 | I2C 地址 / 中断号 / 固件 | UEFI 里先做 I2C 扫描（0x38/0x5d/0x14/0x70） |
| 内核无打印 | entry / 页表 / 串口后端 | `-v` + fb console；先关 SMP |
| PE 卡在某驱动 | ADT 里多了一个不存在的节点 | 二分删 `/arm-io` 子节点 |
| 挂根失败 | 卷头大小端 / 快照 | `fsck_hfs` 校验；换 HFS+ 纯镜像 |
| launchd 秒退 | 型号不匹配 / AMFI | `target-type` 与 images.plist 对齐；关 AMFI |

/*
 * NativeSdhci.cpp — OPPO R11 (SDM660) 的 eMMC 块设备驱动骨架
 *
 * 目标：让 XNU 在 R11 上看到 /dev/disk 级别的块设备，先把根卷挂起来。
 * 现在的实现是**同步 PIO**：单块读写 + 轮询中断状态，故意不碰 DMA/ADMA2/CMDQ/ICE，
 * 因为它们是"挂上根卷之后"的优化，不是 bring-up 的阻塞点（见 docs/hw-r11.md §10）。
 *
 * 匹配：provider 的 `compatible` 含 "qcom,sdhci-msm-v5"（来自 ADT 的 /arm-io/sdhci）。
 *
 * 实测参数（R11）：
 *   core 0xC0C4000 / 0x1000      vendor 0xC0C5000 / 0x1000
 *   IRQ 110、112                 8-bit、VCC 2.95V、VCCQ 1.8V（供电常开）
 *   时钟档 400k/20M/25M/50M/100M/192M/384M，HS400 Enhanced Strobe
 *
 * 编译：由设备仓库的 [kernel] archives 调用
 *   scripts/build-arm-driver.py NativeSdhci
 * 产物 libNativeSdhci.a，再由 CONFIG_XNU_NATIVE_SDHCI=y 链进内核。
 *
 * 状态：探测模式（默认）。start() 里做 reset -> clock -> cardInit -> EXT_CSD -> 读 LBA0/LBA1
 *       并把 GPT 分区表打印到屏幕，用来在真机上验证整条链路。
 *       块设备层（IOBlockStorageDevice）需要 IOStorageFamily 的原版绑定审计，默认关闭，
 *       用 -DXNU_NATIVE_SDHCI_BLOCK_DEVICE=1 打开；写路径、CMD6、ADMA2、中断仍是 TODO。
 */
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IOMemoryDescriptor.h>
/* 内核头文件里没有 IOMemoryMap.h（只有 IOUSer 的）；IOMemoryMap 由
 * IOMemoryDescriptor.h / IODeviceMemory.h 的声明带出来 —— 与 ButtonProbe.cpp 的做法一致。 */
/* 块设备层（IOBlockStorageDevice）的实现体在 IOStorageFamily kext 里，静态链接期
 * 需要"原版绑定审计"生成 .set 绝对地址绑定（见 src/UniXNU/scripts/audit-storage-interface.py
 * 与 build-native-virtio-bindings.py）。bring-up 第一步只要证明 eMMC 能读，
 * 所以默认关掉这一层：驱动退化成纯 IOService 探测器，不引入任何 storage 符号。
 * audit + 绑定打通后再用 -DXNU_NATIVE_SDHCI_BLOCK_DEVICE=1 打开。 */
#ifndef XNU_NATIVE_SDHCI_BLOCK_DEVICE
#define XNU_NATIVE_SDHCI_BLOCK_DEVICE 0
#endif
#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
#include <IOKit/storage/IOBlockStorageDevice.h>
#include <IOKit/storage/IOStorage.h>
#endif
#include <pexpert/pexpert.h>
#include <string.h>

#include "SdhciRegs.h"

#define kSdhciBlockSize   512ULL
#define kSdhciCmdLoops    200000U     /* 轮询上限（约 200 ms，IODelay(1) = 1 us） */
#define kSdhciDataLoops   4000000U    /* 数据阶段上限（约 4 s） */

/* bring-up 版本标记：每改一次驱动就 +1（v3 = 加逐步日志 + GCC 时钟状态回读）。
 * 屏幕上的 `NativeSdhci[v3]:` 前缀就是"刷进去的是这一版"的证据，省得再靠 UUID 猜。 */
#define kSdhciTag         "v3"

/* SDM660 GCC（Global Clock Controller）：eMMC 的 iface/core 时钟由它门控。
 * 偏移来自 drivers/clk/qcom/gcc-sdm660.c：
 *   gcc_sdcc1_apps_clk   enable_reg 0x16004 bit0（parent = sdcc1_apps_clk_src）
 *   gcc_sdcc1_ahb_clk    enable_reg 0x16008 bit0
 *   gcc_sdcc1_ice_core_clk enable_reg 0x1600c bit0
 *   sdcc1_apps_clk_src   cmd_rcgr 0x1602c（CFG_RCGR/M/N/D/CMD_RCGR = +0/+4/+8/+0xC/+0x10）
 * XNU 没有时钟框架，这里先只**读**状态，判断"寄存器访问挂死"是不是因为时钟被门控。 */
#define R11_GCC_PHYS      0x00100000ULL
#define R11_GCC_REG_SIZE  0x00020000ULL
#define GCC_SDCC1_APPS_CBCR 0x16004U
#define GCC_SDCC1_AHB_CBCR  0x16008U
#define GCC_SDCC1_ICE_CBCR  0x1600cU
#define GCC_SDCC1_APPS_RCGR 0x1602cU

/* 诊断计数器，panic 里能看到走到哪一步 */
volatile uint32_t native_sdhci_probe_calls = 0;
volatile uint32_t native_sdhci_matched     = 0;
volatile uint32_t native_sdhci_step        = 0;
volatile uint32_t native_sdhci_caps        = 0;
volatile uint32_t native_sdhci_caps1       = 0;
volatile uint32_t native_sdhci_version     = 0;
volatile uint32_t native_sdhci_rca         = 0;
volatile uint32_t native_sdhci_cid0        = 0;
volatile uint32_t native_sdhci_gcc_apps    = 0;
volatile uint32_t native_sdhci_gcc_cfg     = 0;

static inline uint32_t sdhciRead32 (uintptr_t base, uint32_t off)
{
    return *reinterpret_cast<volatile uint32_t *> (base + off);
}

static inline void sdhciWrite32 (uintptr_t base, uint32_t off, uint32_t value)
{
    *reinterpret_cast<volatile uint32_t *> (base + off) = value;
    /* 让写入先落地，避免后续的 status 轮询读到旧值 */
    (void) sdhciRead32 (base, SDHCI_PRESENT_STATE);
}

static uintptr_t sdhciMapPhys (uint64_t phys, uint64_t length, OSSharedPtr<IOMemoryMap> &keep)
{
    IODeviceMemory *memory = IODeviceMemory::withRange (phys, length);
    if (!memory) {
        return 0;
    }
    keep = OSSharedPtr<IOMemoryMap> (memory->map (kIOMapAnywhere), OSNoRetain);
    memory->release ();
    return keep ? static_cast<uintptr_t> (keep->getVirtualAddress ()) : 0;
}

static bool sdhciWaitSet (uintptr_t base, uint32_t off, uint32_t mask, uint32_t loops)
{
    for (uint32_t i = 0; i < loops; i++) {
        if ((sdhciRead32 (base, off) & mask) == mask) {
            return true;
        }
        IODelay (1);
    }
    return false;
}

#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
class NativeSdhciBlock;
#endif

class NativeSdhci : public IOService
{
    OSDeclareDefaultStructors (NativeSdhci)

public:
    bool start (IOService *provider) override;
    void stop (IOService *provider) override;
    IOService *probe (IOService *provider, SInt32 *score) override;
    void free () override;

    /* 给块设备层用 */
    IOReturn readBlocks (UInt64 block, UInt32 count, void *buffer);
    IOReturn writeBlocks (UInt64 block, UInt32 count, const void *buffer);
    UInt64   deviceBlockCount () const { return fBlockCount; }

private:
    bool     mapRegisters (IOService *provider);
    void     logGccState ();
    bool     softReset (uint32_t which, uint32_t loops);
    bool     setBusPowerAndClock ();
    bool     cardInit ();

    bool     sendCommand (uint32_t index, uint32_t arg, uint32_t flags, uint32_t *resp);
    bool     waitCommandComplete ();
    bool     waitTransferComplete ();
    bool     pioTransfer (bool read, void *buffer, UInt32 bytes);
    bool     readCsd ();
    bool     readExtCsd ();
    bool     probeMedia ();

    uintptr_t fCore     = 0;
    uintptr_t fVendor   = 0;
    OSSharedPtr<IOMemoryMap> fCoreMap;
    OSSharedPtr<IOMemoryMap> fVendorMap;
    UInt16    fRca      = 0;
    UInt64    fBlockCount = 0;
#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
    NativeSdhciBlock *fBlockDevice = nullptr;
#endif
};

#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
class NativeSdhciBlock : public IOBlockStorageDevice
{
    OSDeclareDefaultStructors (NativeSdhciBlock)

public:
    bool init (NativeSdhci *controller);
    bool start (IOService *provider) override;
    void stop (IOService *provider) override;
    void free () override;

    /* ---- IOBlockStorageDevice 纯虚函数（必须全部实现）---- */
    IOReturn doAsyncReadWrite (IOMemoryDescriptor *buffer, UInt64 block, UInt64 nblks,
                               IOStorageAttributes *attributes, IOStorageCompletion *completion) override;
    IOReturn doEjectMedia () override;
    IOReturn doFormatMedia (UInt64 byteCapacity) override;
    UInt32   doGetFormatCapacities (UInt64 *capacities, UInt32 capacitiesMaxCount) const override;
    char    *getVendorString () override;
    char    *getProductString () override;
    char    *getRevisionString () override;
    char    *getAdditionalDeviceInfoString () override;
    IOReturn reportBlockSize (UInt64 *blockSize) override;
    IOReturn reportEjectability (bool *isEjectable) override;
    IOReturn reportMaxValidBlock (UInt64 *maxBlock) override;
    IOReturn reportMediaState (bool *mediaPresent, bool *changedState = 0) override;
    /* reportPollRequirements 仅 TARGET_OS_OSX 存在，iOS 内核里没有这个虚函数 */
    IOReturn reportRemovability (bool *isRemovable) override;
    IOReturn reportWriteProtection (bool *isWriteProtected) override;

    /* ---- 非虚辅助（基类没有这两个虚函数，不能写 override）---- */
    IOReturn doSyncReadWrite (IOMemoryDescriptor *buffer, UInt64 block, UInt64 nblks);

private:
    NativeSdhci *fController = nullptr;
};
#endif /* XNU_NATIVE_SDHCI_BLOCK_DEVICE */

OSDefineMetaClassAndStructors (NativeSdhci, IOService)
#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
OSDefineMetaClassAndStructors (NativeSdhciBlock, IOBlockStorageDevice)
#endif

/* ============================ NativeSdhci ============================ */

/* 小工具：ADT 的 compatible 是 NUL 分隔的多串，这里不依赖 libc 的 strstr */
static bool sdhciContains (const char *text, size_t len, const char *needle)
{
    const size_t n = strlen (needle);
    if (!text || n == 0 || len < n) {
        return false;
    }
    for (size_t i = 0; i + n <= len; i++) {
        size_t k = 0;
        while (k < n && text[i + k] == needle[k]) {
            k++;
        }
        if (k == n) {
            return true;
        }
    }
    return false;
}

IOService *NativeSdhci::probe (IOService *provider, SInt32 *score)
{
    native_sdhci_probe_calls = native_sdhci_probe_calls + 1;

    OSObject *property = provider->getProperty ("compatible");
    bool match = false;
    if (OSData *data = OSDynamicCast (OSData, property)) {
        match = sdhciContains (static_cast<const char *> (data->getBytesNoCopy ()),
                               data->getLength (), "qcom,sdhci-msm-v5");
    } else if (OSString *string = OSDynamicCast (OSString, property)) {
        const char *text = string->getCStringNoCopy ();
        match = text && sdhciContains (text, strlen (text), "qcom,sdhci-msm-v5");
    }

    if (!match) {
        /* 节点里写了 sdhci 但 compatible 不对时也要吼一声，否则真机上完全静默、
         * 分不清"没匹配到"和"匹配到但 start 失败"。 */
        const char *text = nullptr;
        size_t len = 0;
        if (OSData *data = OSDynamicCast (OSData, property)) {
            text = static_cast<const char *> (data->getBytesNoCopy ());
            len = data->getLength ();
        } else if (OSString *string = OSDynamicCast (OSString, property)) {
            text = string->getCStringNoCopy ();
            len = text ? strlen (text) : 0;
        }
        if (text && sdhciContains (text, len, "sdhci")) {
            IOLog ("NativeSdhci[" kSdhciTag "]: probe saw compatible=%.*s but no match\n", (int) (len < 96 ? len : 96), text);
        }
        return nullptr;
    }
    native_sdhci_matched = 1;
    IOLog ("NativeSdhci[" kSdhciTag "]: matched DT node, probing controller\n");
    if (score) {
        *score = 1000;
    }
    return this;
}

bool NativeSdhci::mapRegisters (IOService *provider)
{
    /* 优先用 provider 给的 IODeviceMemory（ADT 的 reg 段），失败则用板级常量 */
    bool ok = false;
    IOLog ("NativeSdhci[" kSdhciTag "]: mapRegisters: provider=%p memCount=%u\n",
           provider, provider ? (unsigned) provider->getDeviceMemoryCount () : 0U);
    if (fCore == 0) {
        if (provider && provider->getDeviceMemoryCount () > 0) {
            if (IODeviceMemory *memory = provider->getDeviceMemoryWithIndex (0)) {
                fCoreMap = OSSharedPtr<IOMemoryMap> (
                    memory->map (kIOMapAnywhere), OSNoRetain);
                if (fCoreMap) {
                    fCore = static_cast<uintptr_t> (fCoreMap->getVirtualAddress ());
                    ok = true;
                }
            }
        }
    }
    if (!ok) {
        fCore = sdhciMapPhys (R11_SDHCI_CORE_PHYS, R11_SDHCI_REG_SIZE, fCoreMap);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: mapRegisters: core va=%p\n", (void *) fCore);
    if (!fCore) {
        return false;
    }
    /* msm v5 的第二段是独立的 vendor 窗口；ADT 里通常作为 reg1 出现 */
    if (provider && provider->getDeviceMemoryCount () > 1) {
        if (IODeviceMemory *memory = provider->getDeviceMemoryWithIndex (1)) {
            fVendorMap = OSSharedPtr<IOMemoryMap> (
                memory->map (kIOMapAnywhere), OSNoRetain);
            if (fVendorMap) {
                fVendor = static_cast<uintptr_t> (fVendorMap->getVirtualAddress ());
            }
        }
    }
    if (!fVendor) {
        fVendor = sdhciMapPhys (R11_SDHCI_VENDOR_PHYS, R11_SDHCI_REG_SIZE, fVendorMap);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: mapRegisters: vendor va=%p\n", (void *) fVendor);
    return fVendor != 0;
}

/* 只读地把 GCC 里 eMMC 相关时钟的开关/分频状态打到屏幕上。
 * 如果 apps_cbcr 显示 CLK_OFF 或 HALT，那么后面第一次读 SDCC 寄存器就可能整机挂死，
 * 这时日志会停在"gcc:"这一行之后 —— 那就能确定下一步必须自己开时钟。 */
void NativeSdhci::logGccState ()
{
    OSSharedPtr<IOMemoryMap> gccMap;
    uintptr_t gcc = sdhciMapPhys (R11_GCC_PHYS, R11_GCC_REG_SIZE, gccMap);
    if (!gcc) {
        IOLog ("NativeSdhci[" kSdhciTag "]: gcc: mapping failed\n");
        return;
    }
    uint32_t apps = sdhciRead32 (gcc, GCC_SDCC1_APPS_CBCR);
    uint32_t ahb  = sdhciRead32 (gcc, GCC_SDCC1_AHB_CBCR);
    uint32_t ice  = sdhciRead32 (gcc, GCC_SDCC1_ICE_CBCR);
    uint32_t cfg  = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR);
    uint32_t m    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + 0x04);
    uint32_t n    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + 0x08);
    uint32_t d    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + 0x0c);
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: apps=%08x ahb=%08x ice=%08x\n", apps, ahb, ice);
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: rcg cfg=%08x m/n/d=%08x/%08x/%08x\n", cfg, m, n, d);
    native_sdhci_gcc_apps = apps;
    native_sdhci_gcc_cfg  = cfg;
}

bool NativeSdhci::softReset (uint32_t which, uint32_t loops)
{
    sdhciWrite32 (fCore, SDHCI_SOFTWARE_RESET, which);
    for (uint32_t i = 0; i < loops; i++) {
        if ((sdhciRead32 (fCore, SDHCI_SOFTWARE_RESET) & which) == 0) {
            return true;
        }
        IODelay (10);
    }
    return false;
}

bool NativeSdhci::setBusPowerAndClock ()
{
    /*
     * R11 上 VCC(2.95V)/VCCQ(1.8V) 都是常开（dmesg: "No vmmc regulator found"），
     * 所以这里只需要：1.8V 信号 + 8-bit 总线 + SDCLK。
     * SDCLK 的父时钟来自 GCC（实测档位最高 384 MHz），先跑 400 kHz 做识别，
     * 识别成功后再升到 200 MHz（HS200）——升频在 cardInit() 之后由 TODO 补。
     */
    uint16_t hostCtrl = (uint16_t) sdhciRead32 (fCore, SDHCI_HOST_CONTROL);
    hostCtrl |= SDHCI_CTRL_8BITBUS;
    hostCtrl &= (uint16_t) ~SDHCI_CTRL_4BITBUS;

    uint16_t hostCtrl2 = (uint16_t) sdhciRead32 (fCore, SDHCI_HOST_CONTROL2);
    hostCtrl2 |= SDHCI_CTRL_VDD_180;             /* eMMC 在 1.8 V 上跑 HS200/HS400 */
    hostCtrl2 &= (uint16_t) ~SDHCI_CTRL_UHS_MASK;
    sdhciWrite32 (fCore, SDHCI_HOST_CONTROL2, hostCtrl2);
    sdhciWrite32 (fCore, SDHCI_HOST_CONTROL, hostCtrl);

    /* 超时计数器取最大 */
    sdhciWrite32 (fCore, SDHCI_TIMEOUT_CONTROL, 0x0E);

    /* 400 kHz 启动：SDCLK = base / (2 * div)。假定 base 50 MHz，div = 62 */
    uint16_t clk = (uint16_t) (SDHCI_CLOCK_INT_EN | SDHCI_CLOCK_SDCLK_DIV (62));
    IOLog ("NativeSdhci[" kSdhciTag "]: clock: hostctl=%04x hostctl2=%04x clk=%04x\n",
           hostCtrl, hostCtrl2, clk);
    sdhciWrite32 (fCore, SDHCI_CLOCK_CONTROL, clk);
    if (!sdhciWaitSet (fCore, SDHCI_CLOCK_CONTROL, SDHCI_CLOCK_INT_STABLE, kSdhciCmdLoops)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: internal clock never stabilised (clk=%08x)\n",
               sdhciRead32 (fCore, SDHCI_CLOCK_CONTROL));
        return false;
    }
    clk |= SDHCI_CLOCK_CARD_EN;
    sdhciWrite32 (fCore, SDHCI_CLOCK_CONTROL, clk);
    IODelay (1000);
    IOLog ("NativeSdhci[" kSdhciTag "]: clock: stable, clk=%08x\n",
           sdhciRead32 (fCore, SDHCI_CLOCK_CONTROL));
    return true;
}

bool NativeSdhci::waitCommandComplete ()
{
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        uint32_t status = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
        if (status & SDHCI_INT_ERROR) {
            uint32_t err = sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS);
            sdhciWrite32 (fCore, SDHCI_ERROR_INT_STATUS, err);
            IOLog ("NativeSdhci[" kSdhciTag "]: cmd error int=%08x err=%08x\n", status, err);
            return false;
        }
        if (status & SDHCI_INT_RESPONSE) {
            sdhciWrite32 (fCore, SDHCI_NORMAL_INT_STATUS, SDHCI_INT_RESPONSE);
            return true;
        }
        IODelay (1);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: cmd timeout state=%08x\n", sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    return false;
}

bool NativeSdhci::waitTransferComplete ()
{
    for (uint32_t i = 0; i < kSdhciDataLoops; i++) {
        uint32_t status = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
        if (status & SDHCI_INT_ERROR) {
            uint32_t err = sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS);
            sdhciWrite32 (fCore, SDHCI_ERROR_INT_STATUS, err);
            IOLog ("NativeSdhci[" kSdhciTag "]: data error int=%08x err=%08x\n", status, err);
            return false;
        }
        if (status & SDHCI_INT_DATA_END) {
            sdhciWrite32 (fCore, SDHCI_NORMAL_INT_STATUS, SDHCI_INT_DATA_END);
            return true;
        }
        IODelay (1);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: data timeout state=%08x\n", sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    return false;
}

bool NativeSdhci::sendCommand (uint32_t index, uint32_t arg, uint32_t flags, uint32_t *resp)
{
    /* 等 CMD/DATA 线空闲（上一次传输还没收尾时不能发新命令） */
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        if (!(sdhciRead32 (fCore, SDHCI_PRESENT_STATE) & (SDHCI_CMD_INHIBIT | SDHCI_DATA_INHIBIT))) {
            break;
        }
        IODelay (1);
    }

    sdhciWrite32 (fCore, SDHCI_NORMAL_INT_STATUS, 0xFFFFFFFF);
    sdhciWrite32 (fCore, SDHCI_ERROR_INT_STATUS, 0xFFFFFFFF);
    sdhciWrite32 (fCore, SDHCI_ARGUMENT, arg);
    sdhciWrite32 (fCore, SDHCI_COMMAND, SDHCI_MAKE_CMD (index, flags));

    if ((flags & SDHCI_CMD_RESP_MASK) == SDHCI_CMD_RESP_NONE) {
        return true;
    }
    if (!waitCommandComplete ()) {
        return false;
    }
    if (resp) {
        resp[0] = sdhciRead32 (fCore, SDHCI_RESPONSE + 0);
        resp[1] = sdhciRead32 (fCore, SDHCI_RESPONSE + 4);
        resp[2] = sdhciRead32 (fCore, SDHCI_RESPONSE + 8);
        resp[3] = sdhciRead32 (fCore, SDHCI_RESPONSE + 12);
    }
    return true;
}

bool NativeSdhci::pioTransfer (bool read, void *buffer, UInt32 bytes)
{
    uint32_t *words = static_cast<uint32_t *> (buffer);
    const UInt32 count = bytes / 4;

    if (read) {
        for (UInt32 i = 0; i < count; i++) {
            if (!sdhciWaitSet (fCore, SDHCI_PRESENT_STATE, SDHCI_DATA_AVAILABLE, kSdhciDataLoops)) {
                IOLog ("NativeSdhci[" kSdhciTag "]: pio read starved at word %u\n", i);
                return false;
            }
            words[i] = sdhciRead32 (fCore, SDHCI_BUFFER);
        }
    } else {
        for (UInt32 i = 0; i < count; i++) {
            if (!sdhciWaitSet (fCore, SDHCI_PRESENT_STATE, SDHCI_SPACE_AVAILABLE, kSdhciDataLoops)) {
                IOLog ("NativeSdhci[" kSdhciTag "]: pio write stalled at word %u\n", i);
                return false;
            }
            sdhciWrite32 (fCore, SDHCI_BUFFER, words[i]);
        }
    }
    return waitTransferComplete ();
}

bool NativeSdhci::readCsd ()
{
    uint32_t resp[4] = {0, 0, 0, 0};
    /* CMD9 读 CSD：最高 3 位是 CSD_STRUCTURE，后 12 位是容量相关字段 */
    if (!sendCommand (MMC_SEND_CSD, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }
    /* 简化：从 CSD v1 的 C_SIZE 估算容量；HS400 卡通常是 CSD v2，留给 EXT_CSD */
    uint32_t cSize = ((resp[2] & 0x3FF) << 2) | ((resp[1] >> 30) & 0x3);
    if (cSize >= 0xFFF) {
        fBlockCount = 0;   /* CSD v2：容量从 EXT_CSD 的 SEC_COUNT 取，TODO */
    } else {
        fBlockCount = ((UInt64) (cSize + 1) * 1024) * 1024 / kSdhciBlockSize;
    }
    return true;
}

/* ---------------- EXT_CSD 与介质探测（bring-up 用，不依赖 storage 层） ---------------- */

static void sdhciCopyLe (uint64_t *out, const uint8_t *raw, size_t bytes)
{
    uint64_t value = 0;
    for (size_t i = 0; i < bytes && i < 8; i++) {
        value |= (uint64_t) raw[i] << (8 * i);
    }
    *out = value;
}

/* GPT 的分区名是 72 字节 UTF-16LE；只保留低字节里的可见 ASCII，够认分区 */
static void sdhciGptName (const uint8_t *entry, char *out, size_t outSize)
{
    size_t k = 0;
    for (size_t i = 0; i < 36 && k + 1 < outSize; i++) {
        uint8_t lo = entry[56 + i * 2];
        if (lo == 0) {
            break;
        }
        out[k++] = (lo >= 0x20 && lo < 0x7f) ? (char) lo : '?';
    }
    out[k] = '\0';
}

/* GPT GUID 前 3 段小端、后 2 段大端，这里按展示顺序重排 */
static void sdhciFormatGuid (const uint8_t *raw, char *out)
{
    static const char hex[] = "0123456789ABCDEF";
    static const uint8_t order[16] = {3,2,1,0, 5,4, 7,6, 8,9, 10,11,12,13,14,15};
    size_t k = 0;
    for (int i = 0; i < 16; i++) {
        if (i == 4 || i == 6 || i == 8 || i == 10) {
            out[k++] = '-';
        }
        uint8_t byte = raw[order[i]];
        out[k++] = hex[byte >> 4];
        out[k++] = hex[byte & 0xF];
    }
    out[k] = '\0';
}

bool NativeSdhci::readExtCsd ()
{
    uint8_t ext[kSdhciBlockSize] __attribute__ ((aligned (64)));
    memset (ext, 0, sizeof (ext));

    sdhciWrite32 (fCore, SDHCI_BLOCK_SIZE, (uint32_t) kSdhciBlockSize);
    sdhciWrite32 (fCore, SDHCI_BLOCK_COUNT, 1);
    sdhciWrite32 (fCore, SDHCI_TRANSFER_MODE, SDHCI_TRNS_READ);

    if (!sendCommand (MMC_SEND_EXT_CSD, 0,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX | SDHCI_CMD_DATA,
                      nullptr)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: CMD8 (SEND_EXT_CSD) failed\n");
        return false;
    }
    if (!pioTransfer (true, ext, (UInt32) kSdhciBlockSize)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: EXT_CSD transfer failed\n");
        return false;
    }

    /* EXT_CSD[212..215] = SEC_COUNT（512 B 扇区总数，eMMC 4.0+） */
    uint64_t secCount = 0;
    sdhciCopyLe (&secCount, ext + 212, 4);
    if (secCount) {
        fBlockCount = secCount;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: EXT_CSD rev=%u SEC_COUNT=%llu (%llu MiB) BUS_WIDTH=%u HS_TIMING=%u\n",
           ext[192], (unsigned long long) secCount,
           (unsigned long long) (secCount / 2048), ext[183], ext[185]);
    return true;
}

bool NativeSdhci::probeMedia ()
{
    uint8_t block[kSdhciBlockSize] __attribute__ ((aligned (64)));

    (void) readExtCsd ();

    memset (block, 0, sizeof (block));
    if (readBlocks (0, 1, block) != kIOReturnSuccess) {
        IOLog ("NativeSdhci[" kSdhciTag "]: LBA0 read FAILED\n");
        return false;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: LBA0 ok sig=%02x%02x%02x%02x%02x%02x%02x%02x\n",
           block[0], block[1], block[2], block[3], block[4], block[5], block[6], block[7]);

    if (block[510] == 0x55 && block[511] == 0xAA) {
        for (int i = 0; i < 4; i++) {
            const uint8_t *entry = block + 446 + i * 16;
            uint64_t start = 0, sectors = 0;
            if (entry[4] == 0) {
                continue;
            }
            sdhciCopyLe (&start, entry + 8, 4);
            sdhciCopyLe (&sectors, entry + 12, 4);
            IOLog ("NativeSdhci[" kSdhciTag "]:   MBR p%d type=0x%02x start=%llu count=%llu\n",
                   i + 1, entry[4], (unsigned long long) start, (unsigned long long) sectors);
        }
    }

    uint8_t header[kSdhciBlockSize] __attribute__ ((aligned (64)));
    memset (header, 0, sizeof (header));
    if (readBlocks (1, 1, header) != kIOReturnSuccess) {
        return true;
    }
    if (memcmp (header, "EFI PART", 8) != 0) {
        IOLog ("NativeSdhci[" kSdhciTag "]: no GPT (LBA1 sig=%02x%02x%02x%02x)\n",
               header[0], header[1], header[2], header[3]);
        return true;
    }

    uint64_t entriesLba = 0, firstUsable = 0, lastUsable = 0, entryCount = 0, entrySize = 0;
    sdhciCopyLe (&firstUsable, header + 0x30, 8);
    sdhciCopyLe (&lastUsable,  header + 0x38, 8);
    sdhciCopyLe (&entriesLba,  header + 0x48, 8);
    sdhciCopyLe (&entryCount,  header + 0x50, 4);
    sdhciCopyLe (&entrySize,   header + 0x54, 4);
    IOLog ("NativeSdhci[" kSdhciTag "]: GPT ok entries=%llu size=%llu table_lba=%llu usable=%llu..%llu\n",
           (unsigned long long) entryCount, (unsigned long long) entrySize,
           (unsigned long long) entriesLba,
           (unsigned long long) firstUsable, (unsigned long long) lastUsable);

    const uint32_t perBlock = entrySize ? (uint32_t) (kSdhciBlockSize / entrySize) : 0;
    if (perBlock == 0) {
        return true;
    }
    uint32_t found = 0, shown = 0;
    for (uint64_t i = 0; i < entryCount && shown < 16; i += perBlock) {
        uint8_t table[kSdhciBlockSize] __attribute__ ((aligned (64)));
        memset (table, 0, sizeof (table));
        if (readBlocks (entriesLba + i / perBlock, 1, table) != kIOReturnSuccess) {
            IOLog ("NativeSdhci[" kSdhciTag "]: GPT table read failed at entry %llu\n", (unsigned long long) i);
            break;
        }
        for (uint32_t j = 0; j < perBlock && i + j < entryCount; j++) {
            const uint8_t *entry = table + j * entrySize;
            bool empty = true;
            for (int k = 0; k < 16; k++) {
                if (entry[k]) {
                    empty = false;
                    break;
                }
            }
            if (empty) {
                continue;
            }
            found++;
            if (shown >= 16) {
                continue;
            }
            char name[40], type[40];
            uint64_t first = 0, last = 0;
            sdhciGptName (entry, name, sizeof (name));
            sdhciFormatGuid (entry, type);
            sdhciCopyLe (&first, entry + 32, 8);
            sdhciCopyLe (&last,  entry + 40, 8);
            IOLog ("NativeSdhci[" kSdhciTag "]:   GPT[%u] %s %llu..%llu type=%s\n",
                   (unsigned) (i + j), name, (unsigned long long) first,
                   (unsigned long long) last, type);
            shown++;
        }
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: GPT non-empty=%u shown=%u\n", found, shown);
    native_sdhci_step = 10;
    return true;
}

bool NativeSdhci::cardInit ()
{
    uint32_t resp[4] = {0, 0, 0, 0};

    native_sdhci_step = 1;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD0\n");
    /* CMD0：回到 idle（此时 CMD 线上不需要 CRC） */
    if (!sendCommand (MMC_GO_IDLE_STATE, 0, SDHCI_CMD_RESP_NONE, nullptr)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: CMD0 failed\n");
        return false;
    }
    IOSleep (10);

    /* CMD1：SEND_OP_COND，直到卡退出 busy（HCS=1 表示支持 1.8V 高容量） */
    native_sdhci_step = 2;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD1 (op cond)\n");
    bool ready = false;
    for (uint32_t i = 0; i < 1000; i++) {
        if (!sendCommand (MMC_SEND_OP_COND, 0x40FF8080,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD1 transfer failed (i=%u)\n", i);
            return false;
        }
        if (resp[0] & 0x80000000U) {
            ready = true;
            break;
        }
        IOSleep (10);
    }
    if (!ready) {
        IOLog ("NativeSdhci[" kSdhciTag "]: card never left busy (CMD1), last=%08x\n", resp[0]);
        return false;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: OCR=%08x\n", resp[0]);

    /* CMD2：拿 CID（长响应），顺便当作"卡还在"的探针 */
    native_sdhci_step = 3;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD2 (cid)\n");
    if (!sendCommand (MMC_ALL_SEND_CID, 0,
                      SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }
    native_sdhci_cid0 = resp[3];

    /* CMD3：分配 RCA（eMMC 由 host 指定） */
    native_sdhci_step = 4;
    fRca = 0x0001;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD3 (rca=1), cid0=%08x\n", native_sdhci_cid0);
    if (!sendCommand (MMC_SET_RELATIVE_ADDR, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }
    native_sdhci_rca = fRca;

    /* CMD9：读 CSD，拿容量 */
    native_sdhci_step = 5;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD9 (csd)\n");
    (void) readCsd ();

    /* CMD7：选中卡 */
    native_sdhci_step = 6;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD7 (select)\n");
    if (!sendCommand (MMC_SELECT_CARD, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_SHORT_BUSY | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }

    /* CMD16：块长 512 */
    native_sdhci_step = 7;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD16 (blklen 512)\n");
    if (!sendCommand (16 /* SET_BLOCKLEN */, (uint32_t) kSdhciBlockSize,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }

    /* TODO: CMD6(SWITCH) 切 EXT_CSD 的 BUS_WIDTH=8 / HS_TIMING=2(HS200)，再升 SDCLK。
     *       现在保持在默认速度，够读第一个块验证链路。 */

    native_sdhci_step = 8;
    return true;
}

IOReturn NativeSdhci::readBlocks (UInt64 block, UInt32 count, void *buffer)
{
    if (count != 1) {
        return kIOReturnUnsupported;   /* TODO: CMD18 + 多块 PIO/ADMA2 */
    }
    sdhciWrite32 (fCore, SDHCI_BLOCK_SIZE, (uint32_t) kSdhciBlockSize);
    sdhciWrite32 (fCore, SDHCI_BLOCK_COUNT, 1);
    sdhciWrite32 (fCore, SDHCI_TRANSFER_MODE, SDHCI_TRNS_READ);

    if (!sendCommand (MMC_READ_SINGLE_BLOCK, (uint32_t) block,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX | SDHCI_CMD_DATA,
                      nullptr)) {
        return kIOReturnTimeout;
    }
    if (!pioTransfer (true, buffer, (UInt32) kSdhciBlockSize)) {
        return kIOReturnError;
    }
    return kIOReturnSuccess;
}

IOReturn NativeSdhci::writeBlocks (UInt64 block, UInt32 count, const void *buffer)
{
    if (count != 1) {
        return kIOReturnUnsupported;
    }
    sdhciWrite32 (fCore, SDHCI_BLOCK_SIZE, (uint32_t) kSdhciBlockSize);
    sdhciWrite32 (fCore, SDHCI_BLOCK_COUNT, 1);
    sdhciWrite32 (fCore, SDHCI_TRANSFER_MODE, 0);

    if (!sendCommand (MMC_WRITE_BLOCK, (uint32_t) block,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX | SDHCI_CMD_DATA,
                      nullptr)) {
        return kIOReturnTimeout;
    }

    /* 写路径**未经实机验证**：先默认拒绝，避免误写坏分区表。修好 PIO 写之后再打开。 */
    return kIOReturnNotWritable;
}

bool NativeSdhci::start (IOService *provider)
{
    IOLog ("NativeSdhci[" kSdhciTag "]: start: enter\n");
    if (!IOService::start (provider)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: start: super::start failed\n");
        return false;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: super ok, mapping registers\n");
    if (!mapRegisters (provider)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: register mapping failed\n");
        goto fail;
    }

    /* 先看时钟有没有被门控：没时钟就动 SDCC 寄存器在 msm 上会挂总线 */
    logGccState ();

    IOLog ("NativeSdhci[" kSdhciTag "]: start: reading version/caps\n");
    native_sdhci_version = sdhciRead32 (fCore, SDHCI_HOST_VERSION) & 0xFF;
    native_sdhci_caps    = sdhciRead32 (fCore, SDHCI_CAPABILITIES);
    native_sdhci_caps1   = sdhciRead32 (fCore, SDHCI_CAPABILITIES_1);
    IOLog ("NativeSdhci[" kSdhciTag "]: ver=%02x caps=%08x caps1=%08x state=%08x\n",
           native_sdhci_version, native_sdhci_caps, native_sdhci_caps1,
           sdhciRead32 (fCore, SDHCI_PRESENT_STATE));

    /* 0x00 / 0xff 都说明控制器没应答（时钟关或地址错），继续下去没意义 */
    if (native_sdhci_version == 0x00 || native_sdhci_version == 0xff) {
        IOLog ("NativeSdhci[" kSdhciTag "]: controller not responding, giving up\n");
        goto fail;
    }

    if (!softReset (SDHCI_RESET_ALL, kSdhciCmdLoops)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: reset timed out\n");
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: reset done\n");
    if (!setBusPowerAndClock ()) {
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: clock done\n");
    if (!cardInit ()) {
        IOLog ("NativeSdhci[" kSdhciTag "]: card init failed at step %u (state=%08x err=%08x)\n",
               native_sdhci_step, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
               sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS));
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: card init done\n");

    native_sdhci_step = 9;
    /* bring-up 第一步：把卡信息 / LBA0 / GPT 分区表打在屏幕上 */
    if (!probeMedia ()) {
        IOLog ("NativeSdhci[" kSdhciTag "]: media probe failed at step %u\n", native_sdhci_step);
        goto fail;
    }

#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
    fBlockDevice = OSTypeAlloc (NativeSdhciBlock);
    if (!fBlockDevice) {
        goto fail;
    }
    if (!fBlockDevice->init (this)) {
        fBlockDevice->release ();
        fBlockDevice = nullptr;
        goto fail;
    }
    if (!fBlockDevice->attach (this)) {
        goto fail;
    }
    fBlockDevice->registerService ();
#else
    IOLog ("NativeSdhci[" kSdhciTag "]: probe mode, %llu blocks, block device NOT registered\n",
           (unsigned long long) fBlockCount);
#endif
    return true;

fail:
    return false;
}

void NativeSdhci::stop (IOService *provider)
{
#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
    if (fBlockDevice) {
        fBlockDevice->terminate (kIOServiceSynchronous);
        fBlockDevice->release ();
        fBlockDevice = nullptr;
    }
#endif
    fCoreMap.reset ();
    fVendorMap.reset ();
    fCore = 0;
    fVendor = 0;
    IOService::stop (provider);
}

void NativeSdhci::free ()
{
    fCoreMap.reset ();
    fVendorMap.reset ();
    IOService::free ();
}

#if XNU_NATIVE_SDHCI_BLOCK_DEVICE
/* ========================= NativeSdhciBlock ========================= */

bool NativeSdhciBlock::init (NativeSdhci *controller)
{
    if (!IOBlockStorageDevice::init (nullptr) || !controller) {
        return false;
    }
    fController = controller;
    return true;
}

bool NativeSdhciBlock::start (IOService *provider)
{
    if (!IOBlockStorageDevice::start (provider)) {
        return false;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: block device online, %llu blocks\n",
           (unsigned long long) fController->deviceBlockCount ());
    return true;
}

void NativeSdhciBlock::stop (IOService *provider)
{
    IOBlockStorageDevice::stop (provider);
}

void NativeSdhciBlock::free ()
{
    fController = nullptr;
    IOBlockStorageDevice::free ();
}

IOReturn NativeSdhciBlock::doAsyncReadWrite (IOMemoryDescriptor *buffer, UInt64 block, UInt64 nblks,
                                             IOStorageAttributes *attributes, IOStorageCompletion *completion)
{
    IOReturn status;

    if (!buffer || nblks == 0) {
        status = kIOReturnBadArgument;
        if (completion) {
            IOStorage::complete (completion, status, 0);
        }
        return status;
    }

    /* 只读实现：写请求直接拒绝（reportWriteProtection 也是只读） */
    if (buffer->getDirection () != kIODirectionIn) {
        if (completion) {
            IOStorage::complete (completion, kIOReturnUnsupported, 0);
        }
        return kIOReturnUnsupported;
    }

    /* 同步 PIO：一次只做一个块，够把根卷挂起来；性能与多块留作 TODO */
    UInt64 remaining = nblks;
    UInt64 current = block;
    status = kIOReturnSuccess;

    while (remaining && status == kIOReturnSuccess) {
        void *scratch = IOMallocAligned (kSdhciBlockSize, 64);
        if (!scratch) {
            status = kIOReturnNoMemory;
            break;
        }
        status = fController->readBlocks (current, 1, scratch);
        if (status == kIOReturnSuccess) {
            buffer->writeBytes ((current - block) * kSdhciBlockSize, scratch, kSdhciBlockSize);
        }
        IOFreeAligned (scratch, kSdhciBlockSize);
        current++;
        remaining--;
    }

    if (completion) {
        IOStorage::complete (completion, status, (nblks - remaining) * kSdhciBlockSize);
    }
    return status;
}

IOReturn NativeSdhciBlock::doSyncReadWrite (IOMemoryDescriptor *buffer, UInt64 block, UInt64 nblks)
{
    return doAsyncReadWrite (buffer, block, nblks, nullptr, nullptr);
}

IOReturn NativeSdhciBlock::doEjectMedia ()
{
    return kIOReturnUnsupported;
}

IOReturn NativeSdhciBlock::doFormatMedia (UInt64 byteCapacity)
{
    return kIOReturnUnsupported;
}

UInt32 NativeSdhciBlock::doGetFormatCapacities (UInt64 *capacities, UInt32 capacitiesMaxCount) const
{
    return 0;
}

IOReturn NativeSdhciBlock::reportBlockSize (UInt64 *blockSize)
{
    if (!blockSize) {
        return kIOReturnBadArgument;
    }
    *blockSize = kSdhciBlockSize;
    return kIOReturnSuccess;
}

IOReturn NativeSdhciBlock::reportEjectability (bool *isEjectable)
{
    if (!isEjectable) {
        return kIOReturnBadArgument;
    }
    *isEjectable = false;
    return kIOReturnSuccess;
}

IOReturn NativeSdhciBlock::reportRemovability (bool *isRemovable)
{
    if (!isRemovable) {
        return kIOReturnBadArgument;
    }
    *isRemovable = false;   /* eMMC 不可拔 */
    return kIOReturnSuccess;
}

IOReturn NativeSdhciBlock::reportWriteProtection (bool *isWriteProtected)
{
    if (!isWriteProtected) {
        return kIOReturnBadArgument;
    }
    *isWriteProtected = true;   /* 写路径未实现前按只读暴露，防止误写 */
    return kIOReturnSuccess;
}


/* ============ 上面这些是 IOBlockStorageDevice 要求的其余纯虚函数 ============ */

char *NativeSdhciBlock::getVendorString ()
{
    return const_cast<char *> ("OPPO");
}

char *NativeSdhciBlock::getProductString ()
{
    return const_cast<char *> ("R11 eMMC");
}

char *NativeSdhciBlock::getRevisionString ()
{
    return const_cast<char *> ("eMMC 5.1");
}

char *NativeSdhciBlock::getAdditionalDeviceInfoString ()
{
    return const_cast<char *> ("NativeSdhci (SDM660 sdhci-msm-v5)");
}

IOReturn NativeSdhciBlock::reportMaxValidBlock (UInt64 *maxBlock)
{
    if (!maxBlock) {
        return kIOReturnBadArgument;
    }
    if (!fController || fController->deviceBlockCount () == 0) {
        return kIOReturnNotReady;
    }
    *maxBlock = fController->deviceBlockCount () - 1;
    return kIOReturnSuccess;
}

IOReturn NativeSdhciBlock::reportMediaState (bool *mediaPresent, bool *changedState)
{
    if (!mediaPresent) {
        return kIOReturnBadArgument;
    }
    /* 卡已经识别成功并且有容量才算介质在位 */
    *mediaPresent = (fController && fController->deviceBlockCount () != 0);
    if (changedState) {
        *changedState = true;
    }
    return kIOReturnSuccess;
}
#endif /* XNU_NATIVE_SDHCI_BLOCK_DEVICE */

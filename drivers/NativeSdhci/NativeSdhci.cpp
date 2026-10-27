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
#define kSdhciCmdLoops    200000U     /* 轮询上限（200000 轮 × 1 us = 200 ms） */
#define kSdhciDataLoops   400000U     /* 数据阶段上限（约 0.4 s；4 s 太长，屏幕上看不到超时行就以为卡死了） */

/* bring-up 版本标记：每改一次驱动就 +1
 * v3 = 逐步日志 + GCC 时钟状态回读
 * v4 = 自己把 sdcc1_apps_clk_src 的根时钟打开（v3 实测 CMD_RCGR=0 → ROOT_EN=0，
 *      分支 CBCR 显示"已开"但根没开 → 读 SDCC 寄存器总线整机挂死）。
 * 屏幕上的 `NativeSdhci[v4]:` 前缀就是"刷进去的是这一版"的证据。 */
#define kSdhciTag         "v20"

/* SDM660 GCC（Global Clock Controller）：eMMC 的 iface/core 时钟由它门控。
 * 偏移来自 drivers/clk/qcom/gcc-sdm660.c：
 *   gcc_sdcc1_apps_clk   enable_reg 0x16004 bit0（parent = sdcc1_apps_clk_src）
 *   gcc_sdcc1_ahb_clk    enable_reg 0x16008 bit0
 *   gcc_sdcc1_ice_core_clk enable_reg 0x1600c bit0
 *   sdcc1_apps_clk_src   cmd_rcgr 0x1602c
 * 寄存器布局取自 drivers/clk/qcom/clk-rcg2.c（clk_rcg2）：
 *   CMD_RCGR +0x00  bit0=CMD_UPDATE bit1=CMD_ROOT_EN bit31=CMD_ROOT_OFF
 *   CFG_RCGR +0x04  bits[4:0]=SRC_DIV bits[10:8]=SRC_SEL bits[13:12]=MODE
 *   M +0x08 / N +0x0c / D +0x10
 * 分支时钟（clk_branch2_ops）：写 CBCR bit0 使能，halt 判定看 CBCR bit31(CLK_OFF)。
 *
 * v3 实测（真机）：apps=0x4221 ahb=0x20008001 → bit31=0 看着都是"开"，
 * 但 CMD_RCGR=0x00000000 → **CMD_ROOT_EN=0**，根时钟根本没输出。
 * 分支"已开"只说明分支门控放行，没有输入时钟照样没有时钟 → 读 SDCC 寄存器 AXI 挂死。
 * 所以这里必须自己把根时钟配好并打开。 */
#define R11_GCC_PHYS      0x00100000ULL
#define R11_GCC_REG_SIZE  0x00020000ULL
#define GCC_SDCC1_APPS_CBCR 0x16004U
#define GCC_SDCC1_AHB_CBCR  0x16008U
#define GCC_SDCC1_ICE_CBCR  0x1600cU
#define GCC_SDCC1_APPS_RCGR 0x1602cU

/* ---- Qualcomm SDCC 的 vendor 寄存器（相对 core 基址 0xC0C4000，见 sdhci-msm.c
 *      的 sdhci_msm_v5_offset）---- */
#define CORE_VENDOR_SPEC           0x20cU
#define CORE_VENDOR_SPEC3          0x250U
#define CORE_ADMA_ERR_ADDR0      0x214U
#define CORE_ADMA_ERR_ADDR1      0x218U

/* ADMA2 用到的连续内存大小（描述符 1 条 = 16 字节，数据 1 块 = 512） */
#define R11_DMA_DESC_SIZE        0x1000U
#define R11_DMA_DATA_SIZE        0x1000U
#define CORE_VENDOR_SPEC_CAP0      0x21cU
#define CORE_MCI_VERSION_REG       0x318U
#define CORE_MCI_STATUS_REG        0x324U
#define CORE_HC_MCLK_SEL_MASK      (3U << 8)
#define CORE_HC_MCLK_SEL_DFLT      (2U << 8)   /* 默认：free running MCLK */
#define CORE_HC_MCLK_SEL_HS400     (3U << 8)   /* HS400：free running MCLK/2 */
#define CORE_HC_SELECT_IN_EN       (1U << 18)
#define CORE_HC_SELECT_IN_MASK     (7U << 19)
#define CORE_IO_PAD_PWR_SWITCH_EN  (1U << 15)
#define CORE_IO_PAD_PWR_SWITCH     (1U << 16)  /* 置 1 = IO 走 1.8V */
#define CORE_3_0V_SUPPORT          (1U << 25)
#define CORE_1_8V_SUPPORT          (1U << 26)
#define CORE_PWRSAVE_DLL           (1U << 3)   /* 在 VENDOR_SPEC3 里 */

#define GCC_RCG_CMD          0x00U
#define GCC_RCG_CFG          0x04U
#define GCC_RCG_M            0x08U
#define GCC_RCG_N            0x0cU
#define GCC_RCG_D            0x10U
#define GCC_RCG_CMD_UPDATE   0x00000001U
#define GCC_RCG_CMD_ROOT_EN  0x00000002U
#define GCC_RCG_CMD_ROOT_OFF 0x80000000U
#define GCC_CBCR_ENABLE      0x00000001U
#define GCC_CBCR_CLK_OFF     0x80000000U

/* 只用 XO（板上 19.2 MHz 晶振，永远在跑）做父时钟，不依赖 GPLL0/GPLL4 有没有起来。
 * SRC_SEL=0 即 XO，SRC_DIV=0 即不分频 → SDCC 基础时钟 19.2 MHz。
 * 这样 bring-up 阶段完全自给自足；等通了再换成 GPLL4/HS200。 */
#define R11_SDCLK_BASE_HZ 19200000U

/* 诊断计数器，panic 里能看到走到哪一步 */
volatile uint32_t native_sdhci_probe_calls = 0;
volatile uint32_t native_sdhci_matched     = 0;
volatile uint32_t native_sdhci_step        = 0;
volatile uint32_t native_sdhci_caps        = 0;
volatile uint32_t native_sdhci_caps1       = 0;
volatile uint32_t native_sdhci_version     = 0;
volatile uint32_t native_sdhci_state       = 0;
volatile uint32_t native_sdhci_rca         = 0;
volatile uint32_t native_sdhci_cid0        = 0;
volatile uint32_t native_sdhci_gcc_apps    = 0;
volatile uint32_t native_sdhci_gcc_cfg     = 0;

/*
 * 访问位宽必须和寄存器定义一致，这一点在 Snapdragon 的 SDCC 上是致命的：
 *   32 位：0x00 ARGUMENT、0x10..0x1C RESPONSE、0x20 BUFFER、0x24 PRESENT_STATE、
 *          0x40 CAPABILITIES、0x44 CAPABILITIES_1、0x48 MAX_CURRENT、0x58 ADMA、0xFC SLOT_INT
 *   16 位：0x04 BLOCK_SIZE、0x06 BLOCK_COUNT、0x0C TRANSFER_MODE、0x0E COMMAND、
 *          0x2C CLOCK_CONTROL、0x30..0x36 中断状态/使能、0x3C AUTO_CMD_STATUS、
 *          0x3E HOST_CONTROL2、**0xFE HOST_VERSION（在 4 字节边界上错位 2 字节）**
 *    8 位：0x28 HOST_CONTROL、0x29 POWER_CONTROL、0x2A BLOCK_GAP、0x2B WAKE_UP、
 *          0x2E TIMEOUT_CONTROL、0x2F SOFTWARE_RESET
 * v3/v4 全程用 32 位读写，第一条 SDCC 访问就是 0xFE 的错位 32 位读，AHB 从口不应答，
 * 整条总线永久挂死（连 panic 都打不出来）。v5 改成正确位宽后 ver/caps 读出来了，
 * 但**8 位访问（0x2F SOFTWARE_RESET）又把总线挂死**，所以 v6 起：
 *   - 16 位寄存器用 16 位访问（0xFE / 0x0E 已验证可用）；
 *   - 8 位寄存器一律不单独访问，凡是落在 4 字节对齐字里的（0x28..0x2B、0x2C..0x2F）
 *     都用一次对齐的 32 位读-改-写完成。
 */
/*
 * v7：不再用 IODelay / IOSleep。
 *
 * v6 的探针实测到 `delay probe: begin` 之后再没有 `delay probe: done` —— 而内核其它线程
 * 照样在跑，说明 IODelay(100) 在这个平台上**不返回**。IODelay 依赖内核认可的时间基准
 * （ARM generic timer 注册成功），而我们这套 ADT 里 pe_arm 连中断控制器都没找到
 * （日志：pe_arm_map_interrupt_controller: failed to find the interrupt-controller），
 * 时间基准没被接管，任何基于它的睡眠都会永久自旋。
 *
 * 这里直接用架构计数寄存器（CNTPCT_EL0 / CNTFRQ_EL0），不依赖内核的任何定时器状态。
 * 计数器不走（freq 为 0）时退化成固定次数自旋，仍然能保证给控制器留出间隔。
 */
static inline uint64_t sdhciCounter (void)
{
    uint64_t v;
    __asm__ volatile ("isb; mrs %0, cntpct_el0" : "=r" (v));
    return v;
}

static inline uint64_t sdhciCounterFreq (void)
{
    uint64_t v;
    __asm__ volatile ("mrs %0, cntfrq_el0" : "=r" (v));
    return v;
}

static void sdhciDelayUs (uint32_t us)
{
    uint64_t freq = sdhciCounterFreq ();
    if (freq == 0 || freq > 1000000000ULL) {
        /* 计数器不可用：固定次数 yield 自旋（asm volatile 保证循环不被优化掉） */
        uint32_t spins = us * 64u;
        for (uint32_t i = 0; i < spins; i++) {
            __asm__ volatile ("yield");
        }
        return;
    }
    uint64_t ticks = (freq / 1000000ULL) * (uint64_t) us + 1;
    uint64_t start = sdhciCounter ();
    uint32_t guard = 0;
    while (sdhciCounter () - start < ticks) {
        __asm__ volatile ("yield");
        if (++guard > 40000000u) {
            break;
        }
    }
}

static inline uint32_t sdhciRead32 (uintptr_t base, uint32_t off)
{
    return *reinterpret_cast<volatile uint32_t *> (base + off);
}

static inline uint16_t sdhciRead16 (uintptr_t base, uint32_t off)
{
    return *reinterpret_cast<volatile uint16_t *> (base + off);
}

static inline void sdhciWrite32 (uintptr_t base, uint32_t off, uint32_t value)
{
    *reinterpret_cast<volatile uint32_t *> (base + off) = value;
    /* 回读同一个寄存器让写入先落地；BUFFER 是 FIFO，回读会吃掉一个数据字 */
    if (off != SDHCI_BUFFER) {
        (void) sdhciRead32 (base, off);
    }
}

static inline void sdhciWrite16 (uintptr_t base, uint32_t off, uint16_t value)
{
    *reinterpret_cast<volatile uint16_t *> (base + off) = value;
    (void) sdhciRead16 (base, off);
}

/* 0x28..0x2B：HOST_CONTROL(8b) + POWER_CONTROL(8b) + BLOCK_GAP(8b) + WAKE_UP(8b) */
static inline uint32_t sdhciReadHostWord (uintptr_t base)
{
    return sdhciRead32 (base, SDHCI_HOST_CONTROL);
}

/* 0x2C..0x2F：CLOCK_CONTROL(16b) + TIMEOUT_CONTROL(8b) + SOFTWARE_RESET(8b) */
static inline uint32_t sdhciReadClockWord (uintptr_t base)
{
    return sdhciRead32 (base, SDHCI_CLOCK_CONTROL);
}

static inline void sdhciWriteClockWord (uintptr_t base, uint32_t word)
{
    sdhciWrite32 (base, SDHCI_CLOCK_CONTROL, word);
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
        sdhciDelayUs (1);
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
    bool     enableGccClocks ();
    bool     softReset (uint32_t which, uint32_t loops);
    void     recoverCommandState (const char *why);
    bool     setBusPowerAndClock ();
    bool     configureVendorSpec ();
    bool     setGccSdccRate (uint32_t m, uint32_t n, uint32_t hidDiv);
    bool     setBusClock (uint32_t hz);
    bool     cardInit ();

    bool     sendCommand (uint32_t index, uint32_t arg, uint32_t flags, uint32_t *resp);
    bool     waitCommandComplete ();
    bool     waitTransferComplete ();
    bool     pioTransfer (bool read, void *buffer, UInt32 bytes);
    bool     setupDmaMemory ();
    bool     beginDmaRead (UInt32 bytes);
    bool     endDmaRead (void *dst, UInt32 bytes);
    bool     readCsd ();
    bool     readExtCsd ();
    bool     probeMedia ();

    uintptr_t fCore     = 0;
    uintptr_t fVendor   = 0;
    OSSharedPtr<IOMemoryMap> fCoreMap;
    OSSharedPtr<IOMemoryMap> fVendorMap;

    /* ADMA2：描述符与数据都必须是物理连续内存，start() 里一次性分配 */
    IOBufferMemoryDescriptor *fDmaDesc     = nullptr;
    IOBufferMemoryDescriptor *fDmaData     = nullptr;
    IOPhysicalAddress         fDmaDescPhys = 0;
    IOPhysicalAddress         fDmaDataPhys = 0;
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
 * 注意寄存器含义（clk-rcg2.h）：cmd_rcgr+0 是 CMD_RCGR（不是 CFG），+4 才是 CFG。 */
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
    uint32_t cmd  = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + GCC_RCG_CMD);
    uint32_t cfg  = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + GCC_RCG_CFG);
    uint32_t m    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + GCC_RCG_M);
    uint32_t n    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + GCC_RCG_N);
    uint32_t d    = sdhciRead32 (gcc, GCC_SDCC1_APPS_RCGR + GCC_RCG_D);
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: apps=%08x ahb=%08x ice=%08x\n", apps, ahb, ice);
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: root cmd=%08x cfg=%08x src=%u div=%u m/n/d=%08x/%08x/%08x\n",
           cmd, cfg, (cfg >> 8) & 0x7, cfg & 0x1f, m, n, d);
    native_sdhci_gcc_apps = apps;
    native_sdhci_gcc_cfg  = cmd;
}

/* 把 sdcc1_apps_clk_src 的根时钟打开，并确保 apps/ahb 两个分支门控放行。
 * 顺序照抄 clk-rcg2.c：先 root off → 写 CFG/M/N/D → CMD_UPDATE 生效 → CMD_ROOT_EN 开根
 * → 轮询 CMD_ROOT_OFF 清零；最后逐个开分支并轮询 CBCR 的 CLK_OFF(bit31) 清零。 */
bool NativeSdhci::enableGccClocks ()
{
    OSSharedPtr<IOMemoryMap> gccMap;
    uintptr_t gcc = sdhciMapPhys (R11_GCC_PHYS, R11_GCC_REG_SIZE, gccMap);
    if (!gcc) {
        IOLog ("NativeSdhci[" kSdhciTag "]: gcc: mapping failed, cannot enable clocks\n");
        return false;
    }
    const uint32_t rcgr = GCC_SDCC1_APPS_RCGR;

    /* 1) 先关根，避免半配置状态输出时钟 */
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, 0);
    /* 2) 父时钟选 XO、不分频、单边沿模式；M/N/D 全清（纯分频模式，输出 = 19.2 MHz） */
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CFG, 0);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_M, 0);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_N, 0);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_D, 0);
    /* 3) CMD_UPDATE 让上面的配置生效 */
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, GCC_RCG_CMD_UPDATE);
    /* 4) 打开根输出 */
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, GCC_RCG_CMD_ROOT_EN);

    bool rootOn = false;
    for (uint32_t i = 0; i < 1000; i++) {
        uint32_t cmd = sdhciRead32 (gcc, rcgr + GCC_RCG_CMD);
        if ((cmd & GCC_RCG_CMD_ROOT_OFF) == 0) {
            rootOn = true;
            break;
        }
        sdhciDelayUs (10);
    }
    uint32_t cmdNow = sdhciRead32 (gcc, rcgr + GCC_RCG_CMD);
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: root enabled -> cmd=%08x%s\n", cmdNow,
           rootOn ? "" : "  (ROOT_OFF still set!)");

    /* 5) 分支门控：先读改写再判定，别把别人的位清掉 */
    struct { uint32_t off; const char *name; } branches[] = {
        { GCC_SDCC1_AHB_CBCR,  "ahb"  },
        { GCC_SDCC1_APPS_CBCR, "apps" },
    };
    bool allOn = true;
    for (unsigned i = 0; i < sizeof (branches) / sizeof (branches[0]); i++) {
        uint32_t v = sdhciRead32 (gcc, branches[i].off);
        sdhciWrite32 (gcc, branches[i].off, v | GCC_CBCR_ENABLE);
        bool on = false;
        for (uint32_t k = 0; k < 1000; k++) {
            if ((sdhciRead32 (gcc, branches[i].off) & GCC_CBCR_CLK_OFF) == 0) {
                on = true;
                break;
            }
            sdhciDelayUs (10);
        }
        IOLog ("NativeSdhci[" kSdhciTag "]: gcc: branch %s=%08x%s\n", branches[i].name,
               sdhciRead32 (gcc, branches[i].off), on ? "" : "  (CLK_OFF stuck)");
        allOn = allOn && on;
    }
    return rootOn && allOn;
}

bool NativeSdhci::softReset (uint32_t which, uint32_t loops)
{
    /* SOFTWARE_RESET 是 0x2F 的 8 位寄存器，落在 0x2C 这个 32 位字的最高字节里，
     * 用整字读写访问（v6 起不再有任何 8 位访问）。 */
    uint32_t before = sdhciReadClockWord (fCore);
    sdhciWriteClockWord (fCore, (before & 0x00FFFFFFu) | (((uint32_t) which) << 24));
    IOLog ("NativeSdhci[" kSdhciTag "]: reset: assert=%02x word %08x -> %08x\n",
           which, before, sdhciReadClockWord (fCore));

    for (uint32_t i = 0; i < loops; i++) {
        if (((sdhciReadClockWord (fCore) >> 24) & which) == 0) {
            IOLog ("NativeSdhci[" kSdhciTag "]: reset: cleared after %u polls, word=%08x\n",
                   i, sdhciReadClockWord (fCore));
            return true;
        }
        sdhciDelayUs (10);
    }

    /*
     * v7 实测：写 RESET_ALL(0x01) 之后 200 ms 都不清零（读回 `01000003`），
     * 而 ABL 交接时 CLOCK_CONTROL 已经是 `000f0007`（内部时钟已稳、卡时钟已开）——
     * 全复位会把卡时钟关掉、在这套"bootloader 已经初始化好控制器"的环境里走不回来。
     * 所以复位不再是致命错误：强制把复位位写回 0，然后继续做 power + clock + CMD。
     */
    uint32_t stuck = sdhciReadClockWord (fCore);
    sdhciWriteClockWord (fCore, stuck & 0x00FFFFFFu);
    uint32_t forced = sdhciReadClockWord (fCore);
    IOLog ("NativeSdhci[" kSdhciTag "]: reset: never cleared (word=%08x), forced 0 -> %08x, continuing\n",
           stuck, forced);
    return true;
}

/* 命令状态机复位：v12 真机实测，CMD1 完成时 NIS 里带着"幽灵 error 汇总位"，
 * 我们清掉 NIS 之后控制器内部似乎还留着错误锁存，下一条命令（CMD2）就卡在
 * CMD_INHIBIT 上既不完成也不报错。做法是先记日志，再复位 CMD/DATA 状态机
 * 并清空两个中断状态寄存器，让后续命令能正常下发。 */
void NativeSdhci::recoverCommandState (const char *why)
{
    IOLog ("NativeSdhci[" kSdhciTag "]: recover: %s (state=%08x nis=%04x eis=%04x mstat=%08x)\n",
           why, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
           sdhciRead16 (fCore, SDHCI_NORMAL_INT_STATUS),
           sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS),
           sdhciRead32 (fCore, CORE_MCI_STATUS_REG));
    sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, (uint16_t) 0xFFFF);
    sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, (uint16_t) 0xFFFF);
    (void) softReset (SDHCI_RESET_CMD | SDHCI_RESET_DATA, kSdhciCmdLoops);
    sdhciDelayUs (1000);
    IOLog ("NativeSdhci[" kSdhciTag "]: recover: done (state=%08x)\n",
           sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
}

bool NativeSdhci::setBusPowerAndClock ()
{
    /*
     * R11 上 VCC(2.95V)/VCCQ(1.8V) 都是常开（dmesg: "No vmmc regulator found"），
     * 所以这里只需要：SD 总线电源 + 总线宽度 + SDCLK。
     * SDCLK 的父时钟来自 GCC（实测档位最高 384 MHz），先跑 400 kHz 做识别，
     * 识别成功后再升频（HS200 需要在 CMD6 之后，TODO）。
     */
    /*
     * eMMC 上电默认是 1-bit 总线：DAT1..DAT7 要等 CMD6(SWITCH, EXT_CSD[183] BUS_WIDTH=2)
     * 之后才能用。所以识别阶段保持 1-bit，等 cardInit 成功再切（TODO）。
     * HOST_CONTROL2 的 1.8V Signaling Enable 是 SD 卡 UHS 用的，eMMC 不应置位。
     */
    uint32_t hostWord = sdhciReadHostWord (fCore);
    uint32_t hostCtrl = hostWord & 0xFFu;
    hostCtrl &= ~(uint32_t) (SDHCI_CTRL_8BITBUS | SDHCI_CTRL_4BITBUS);

    /* ABL 交接时 HOST_CONTROL 的 DMA Select 是 ADMA2(64-bit)（实测 0x18）。
     * v15 试过清零（期望强制 PIO），真机结果反而更糟：CMD8 一发出整机静默，
     * 连命令阶段的日志都出不来 —— 说明 MSM 在 DMA Select=SDMA(00) 时会真的启动
     * SDMA 引擎去 0x58 里的野指针搬运，把 AHB 挂死。所以这里**保持**原值，
     * 改成每次数据命令前把 0x58 清零（见 sendCommand）。 */
    IOLog ("NativeSdhci[" kSdhciTag "]: bus: hostctl=%02x (DMA select=%u, keep as-is)\n",
           hostCtrl & 0xFFu, (hostCtrl >> 3) & 0x3u);

    uint16_t hostCtrl2 = sdhciRead16 (fCore, SDHCI_HOST_CONTROL2);
    hostCtrl2 &= (uint16_t) ~(SDHCI_CTRL_VDD_180 | SDHCI_CTRL_UHS_MASK);
    sdhciWrite16 (fCore, SDHCI_HOST_CONTROL2, hostCtrl2);

    /* HOST_CONTROL(0x28) 和 POWER_CONTROL(0x29) 都在 0x28 这个字里，一次 32 位写搞定：
     * 低字节 = HOST_CONTROL，次字节 = POWER_CONTROL(BUS_POWER_ON|3.3V)。
     * R11 上 VCC/VCCQ 常开，这一步只是让控制器开始驱动时钟。 */
    hostWord = (hostWord & 0xFFFF0000u) | ((uint32_t) (SDHCI_POWER_ON | SDHCI_POWER_330) << 8) | hostCtrl;
    sdhciWrite32 (fCore, SDHCI_HOST_CONTROL, hostWord);
    sdhciDelayUs (1000);
    IOLog ("NativeSdhci[" kSdhciTag "]: bus: hostctl2=%04x word28=%08x (1-bit, 3.3V)\n",
           hostCtrl2, sdhciReadHostWord (fCore));

    /* 超时计数器取最大（0x2E，在 0x2C 字的次高字节） */
    /* 超时计数器取最大（0x2E，0x2C 字的次高字节）；复位字节保持 0 */
    sdhciWriteClockWord (fCore, (sdhciReadClockWord (fCore) & 0x0000FFFFu) | (0x0Eu << 16));

    /* 识别阶段 400 kHz：靠 GCC 的 M/N(=1/48) 得到，SDHCI 分频器在本平台上无效 */
    return setBusClock (400000);
}

/* Qualcomm SDCC 的"默认模式"配置，照抄 sdhci-msm.c 的 msm_hc_select_default()：
 *  - CORE_HC_MCLK_SEL 必须是 DFLT(2)，HS400 的 (3) 是 free running MCLK/2；
 *    ABL 交接时控制器可能还停在 HS400 路径上，400 kHz 走那条路是没有输出的。
 *  - CORE_HC_SELECT_IN_EN / MASK 要清零，让 Host Control2 的 UHS 位说了算。
 *  - IO 电平：R11 的 VCCQ 是 1.8V，CORE_1_8V_SUPPORT 置位时 IO_PAD_PWR_SWITCH 要置 1，
 *    否则控制器的输入门限按 3.0V 判，卡在 1.8V 上拉回来的高电平会被读成低电平。
 *  - CORE_PWRSAVE_DLL（VENDOR_SPEC3 bit3）清掉，避免 MCLK 门控时序问题。 */
bool NativeSdhci::configureVendorSpec ()
{
    uint32_t vs0   = sdhciRead32 (fCore, CORE_VENDOR_SPEC);
    uint32_t caps0 = sdhciRead32 (fCore, CORE_VENDOR_SPEC_CAP0);
    uint32_t vs3   = sdhciRead32 (fCore, CORE_VENDOR_SPEC3);
    uint32_t mci   = sdhciRead32 (fCore, CORE_MCI_VERSION_REG);
    uint32_t mstat = sdhciRead32 (fCore, CORE_MCI_STATUS_REG);
    IOLog ("NativeSdhci[" kSdhciTag "]: vendor: mci=%08x (core %u.%02x) vs0=%08x caps0=%08x vs3=%08x mstat=%08x\n",
           mci, (mci >> 28) & 0xF, mci & 0xFF, vs0, caps0, vs3, mstat);

    uint32_t next = vs0;
    next &= ~CORE_HC_MCLK_SEL_MASK;
    next |= CORE_HC_MCLK_SEL_DFLT;
    next &= ~CORE_HC_SELECT_IN_EN;
    next &= ~CORE_HC_SELECT_IN_MASK;

    bool io18 = (caps0 & CORE_1_8V_SUPPORT) != 0;
    if (io18) {
        next |= CORE_IO_PAD_PWR_SWITCH;
    } else {
        next &= ~CORE_IO_PAD_PWR_SWITCH;
    }
    if (next != vs0) {
        sdhciWrite32 (fCore, CORE_VENDOR_SPEC, next);
    }

    uint32_t next3 = vs3 & ~CORE_PWRSAVE_DLL;
    if (next3 != vs3) {
        sdhciWrite32 (fCore, CORE_VENDOR_SPEC3, next3);
    }

    uint32_t vs0Now = sdhciRead32 (fCore, CORE_VENDOR_SPEC);
    uint32_t vs3Now = sdhciRead32 (fCore, CORE_VENDOR_SPEC3);
    IOLog ("NativeSdhci[" kSdhciTag "]: vendor: io18=%u mclk_sel=%u select_in=%u vs0=%08x vs3=%08x\n",
           io18 ? 1 : 0, (vs0Now >> 8) & 3, (vs0Now >> 19) & 7, vs0Now, vs3Now);
    return true;
}

/* GCC 里 sdcc1_apps_clk_src 的输出频率按 clk-rcg2.c 的 calc_rate 反推：
 *   rate = XO * 2/(hid+1) * m/n        （m != n 时走 dual-edge 模式）
 * 400 kHz = 19.2 MHz * 1/48 → m=1, n=48, hid=0；19.2 MHz 就是 m/n=1/1（mode=0 时忽略 m/n）。
 * M/N/D 三个寄存器存的都是反码：M=m、N=~(n-m)、D=~clamp(n, m, 2(n-m))。 */
bool NativeSdhci::setGccSdccRate (uint32_t m, uint32_t n, uint32_t hidDiv)
{
    OSSharedPtr<IOMemoryMap> gccMap;
    uintptr_t gcc = sdhciMapPhys (R11_GCC_PHYS, R11_GCC_REG_SIZE, gccMap);
    if (!gcc) {
        IOLog ("NativeSdhci[" kSdhciTag "]: gcc: mapping failed, cannot set rate\n");
        return false;
    }
    const uint32_t rcgr = GCC_SDCC1_APPS_RCGR;
    const uint32_t mask = 0xFFu;

    if (m == 0) {
        m = 1;
    }
    if (n < m) {
        n = m;
    }

    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, 0);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_M, m & mask);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_N, (~(n - m)) & mask);

    uint32_t dval = n;
    uint32_t twice = (n - m) * 2;
    if (twice > m && dval > twice) {
        dval = twice;
    }
    sdhciWrite32 (gcc, rcgr + GCC_RCG_D, (~dval) & mask);

    uint32_t cfg = hidDiv & 0x1Fu;
    if (m != n) {
        cfg |= (0x2u << 12);        /* CFG_MODE_DUAL_EDGE */
    }
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CFG, cfg);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, GCC_RCG_CMD_UPDATE);
    sdhciWrite32 (gcc, rcgr + GCC_RCG_CMD, GCC_RCG_CMD_ROOT_EN);

    for (uint32_t i = 0; i < 1000; i++) {
        if ((sdhciRead32 (gcc, rcgr + GCC_RCG_CMD) & GCC_RCG_CMD_ROOT_OFF) == 0) {
            break;
        }
        sdhciDelayUs (10);
    }

    /* 回读并按 calc_rate 自己算一遍，屏幕上看得到实际频率 */
    uint32_t cfgNow = sdhciRead32 (gcc, rcgr + GCC_RCG_CFG);
    uint32_t mNow   = sdhciRead32 (gcc, rcgr + GCC_RCG_M) & mask;
    uint32_t nNow   = ((~sdhciRead32 (gcc, rcgr + GCC_RCG_N)) & mask) + mNow;
    uint32_t hidNow = cfgNow & 0x1Fu;
    uint32_t hz = R11_SDCLK_BASE_HZ;
    if (hidNow) {
        hz = (hz * 2) / (hidNow + 1);
    }
    if ((cfgNow & 0x3000u) && nNow) {
        hz = (uint32_t) (((uint64_t) hz * mNow) / nNow);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: gcc: m/n/hid=%u/%u/%u -> %u Hz (cfg=%08x m=%08x n=%08x d=%08x)\n",
           mNow, nNow, hidNow, hz, cfgNow,
           sdhciRead32 (gcc, rcgr + GCC_RCG_M),
           sdhciRead32 (gcc, rcgr + GCC_RCG_N),
           sdhciRead32 (gcc, rcgr + GCC_RCG_D));
    return true;
}

/* 换 SD 总线时钟。**关键**：sdhci-msm.c 明确写着
 * "MSM controller does not use internal divider and instead directly control
 *  the GCC clock as per HW recommendation"，所以 SDHCI 的 CLOCK_CONTROL 分频字段
 * 是无效的 —— 真正决定 SDCLK 的是 sdcc1_apps_clk_src 的 M/N/D（见上面 helper）。
 * r11 上的父时钟只有 XO 19.2 MHz 是确定在跑的，所以用 m/n 凑频率。 */
bool NativeSdhci::setBusClock (uint32_t hz)
{
    if (hz == 0) {
        hz = R11_SDCLK_BASE_HZ;
    }
    uint32_t n = 1;
    if (hz < R11_SDCLK_BASE_HZ) {
        n = R11_SDCLK_BASE_HZ / hz;
        if (n < 1) {
            n = 1;
        }
        if (n > 255) {
            n = 255;                /* mnd_width = 8 */
        }
    } else {
        hz = R11_SDCLK_BASE_HZ;
    }
    uint32_t m = 1;                 /* 分子固定 1，靠 n 分频到目标频率 */

    if (!setGccSdccRate (m, n, 0)) {
        return false;
    }

    uint32_t tail = sdhciReadClockWord (fCore) & 0x00FF0000u;
    sdhciWriteClockWord (fCore, tail);
    sdhciDelayUs (100);
    sdhciWriteClockWord (fCore, tail | SDHCI_CLOCK_INT_EN);
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        if (sdhciReadClockWord (fCore) & SDHCI_CLOCK_INT_STABLE) {
            break;
        }
        sdhciDelayUs (10);
    }
    uint32_t st = sdhciReadClockWord (fCore);
    sdhciWriteClockWord (fCore, tail | SDHCI_CLOCK_INT_EN | SDHCI_CLOCK_CARD_EN);
    sdhciDelayUs (1000);
    IOLog ("NativeSdhci[" kSdhciTag "]: clock: %u Hz (gcc m/n=%u/%u) int=%08x now=%08x\n",
           hz, m, n, st, sdhciReadClockWord (fCore));
    return true;
}

bool NativeSdhci::waitCommandComplete ()
{
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        /* 0x30(Normal) 与 0x32(Error) 是相邻的两个 16 位寄存器，一次对齐 32 位读
         * 就能同时拿到：低 16 位 = NIS，高 16 位 = EIS。另外再单独按 16 位读一次
         * EIS 做交叉验证 —— v10/v11 实测这颗控制器给出的组合是
         * "NIS = 0x8001（Command Complete + Error 汇总位）、EIS = 0x0000"，
         * 汇总位亮了却没有具体错误源，只能当成"命令已完成"处理。 */
        uint32_t word = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
        uint16_t nis = (uint16_t) (word & 0xFFFFu);
        uint16_t eis = (uint16_t) (word >> 16);
        uint16_t eis16 = sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS);

        /* 先判"命令完成"：bit0 置位就说明控制器把这条命令走完了。
         * 只把"没完成却报错"当失败 —— 这样 CMD1 即便带着假的 error 汇总位，
         * 也能走到读 RESPONSE 那一步，把 OCR 打出来。 */
        if (nis & SDHCI_INT_RESPONSE) {
            if (nis & SDHCI_INT_ERROR) {
                IOLog ("NativeSdhci[" kSdhciTag "]: cmd done, error flag ignored: word=%08x nis=%04x eis=%04x eis16=%04x\n",
                       word, nis, eis, eis16);
            }
            sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, nis);
            sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, (uint16_t) 0xFFFF);
            return true;
        }
        if (nis & SDHCI_INT_ERROR) {
            IOLog ("NativeSdhci[" kSdhciTag "]: cmd error before complete: word=%08x nis=%04x eis=%04x eis16=%04x\n",
                   word, nis, eis, eis16);
            sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, nis);
            sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, (uint16_t) 0xFFFF);
            return false;
        }
        sdhciDelayUs (1);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: cmd timeout state=%08x\n", sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    return false;
}

bool NativeSdhci::waitTransferComplete ()
{
    for (uint32_t i = 0; i < kSdhciDataLoops; i++) {
        uint16_t status = sdhciRead16 (fCore, SDHCI_NORMAL_INT_STATUS);
        if (status & SDHCI_INT_ERROR) {
            uint16_t err = sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS);
            sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, err);
            IOLog ("NativeSdhci[" kSdhciTag "]: data error int=%04x err=%04x\n", status, err);
            return false;
        }
        if (status & SDHCI_INT_DATA_END) {
            sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, SDHCI_INT_DATA_END);
            return true;
        }
        sdhciDelayUs (1);
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: data timeout nis=%04x eis=%04x state=%08x\n",
           sdhciRead16 (fCore, SDHCI_NORMAL_INT_STATUS),
           sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS),
           sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    return false;
}

bool NativeSdhci::sendCommand (uint32_t index, uint32_t arg, uint32_t flags, uint32_t *resp)
{
    const bool isData = (flags & SDHCI_CMD_DATA) != 0;

    /* 等 CMD/DATA 线空闲（上一次传输还没收尾时不能发新命令） */
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        if (!(sdhciRead32 (fCore, SDHCI_PRESENT_STATE) & (SDHCI_CMD_INHIBIT | SDHCI_DATA_INHIBIT))) {
            break;
        }
        sdhciDelayUs (1);
    }

    if (isData) {
        /* MSM 的 SDMA/ADMA 地址寄存器（0x58）在 ABL 之后是野值。DMA Enable 虽然是 0，
         * 但保险起见每次数据命令之前清零，避免控制器拿野指针去搬运。 */
        sdhciWrite32 (fCore, SDHCI_ADMA_ADDRESS, 0);
        IOLog ("NativeSdhci[" kSdhciTag "]: cmd%u pre state=%08x adma=%08x\n",
               index, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
               sdhciRead32 (fCore, SDHCI_ADMA_ADDRESS));
    }

    sdhciWrite16 (fCore, SDHCI_NORMAL_INT_STATUS, 0xFFFF);
    sdhciWrite16 (fCore, SDHCI_ERROR_INT_STATUS, 0xFFFF);
    sdhciWrite32 (fCore, SDHCI_ARGUMENT, arg);
    sdhciWrite16 (fCore, SDHCI_COMMAND, SDHCI_MAKE_CMD (index, flags));

    if (isData) {
        /* 打出这一行就说明"写 COMMAND 寄存器"本身没有把总线挂死 */
        IOLog ("NativeSdhci[" kSdhciTag "]: cmd%u posted state=%08x\n",
               index, sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    }

    if ((flags & SDHCI_CMD_RESP_MASK) == SDHCI_CMD_RESP_NONE) {
        return true;
    }
    if (!waitCommandComplete ()) {
        return false;
    }
    if (isData) {
        IOLog ("NativeSdhci[" kSdhciTag "]: cmd%u complete state=%08x\n",
               index, sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
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
        /* 不再"读满块长就收工"：控制器愿意给多少字就收多少字（上限 4 倍块长），
         * 这样能直接量出这次传输到底有多少数据 —— v18 实测读满 128 个字之后
         * BUFFER_READ_ENABLE(bit11) 仍然是 1，说明控制器认为还有数据。 */
        const UInt32 cap = count * 4;
        UInt32 n = 0;
        uint32_t first[4] = { 0, 0, 0, 0 };
        uint32_t last = 0;

        for (UInt32 i = 0; i < cap; i++) {
            if (!sdhciWaitSet (fCore, SDHCI_PRESENT_STATE, SDHCI_DATA_AVAILABLE, kSdhciDataLoops)) {
                if (n == 0) {
                    IOLog ("NativeSdhci[" kSdhciTag "]: pio read starved at word 0/%u state=%08x nis=%08x\n",
                           count, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
                           sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS));
                    return false;
                }
                break;
            }
            uint32_t w = sdhciRead32 (fCore, SDHCI_BUFFER);
            if (n < 4) {
                first[n] = w;
            }
            if (n < count) {
                words[n] = w;
            }
            last = w;
            n++;
        }

        IOLog ("NativeSdhci[" kSdhciTag "]: pio got %u words (block=%u) first=%08x %08x %08x %08x last=%08x\n",
               n, count, first[0], first[1], first[2], first[3], last);
        IOLog ("NativeSdhci[" kSdhciTag "]: pio after state=%08x nis=%08x eis=%08x\n",
               sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
               sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS),
               sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS));
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
    /* CMD9 读 CSD：最高 3 位是 CSD_STRUCTURE，后 12 位是容量相关字段。
     * 同样是 R2 长响应 → 不设 SDHCI_CMD_INDEX。 */
    if (!sendCommand (MMC_SEND_CSD, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC, resp)) {
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

/* ---- ADMA2 -------------------------------------------------------------
 * 描述符格式（64 位 ADMA2，每条 16 字节）：
 *   word0 = attribute(bits 0..15) | length(bits 16..31)
 *   word1 = address[31:0]
 *   word2 = address[63:32]
 *   word3 = 保留
 * attribute: bit0 Valid、bit1 End、bit2 Int、bits4..5 Act（0b10 = TRAN）
 */
bool NativeSdhci::setupDmaMemory ()
{
    if (fDmaDesc && fDmaData) {
        return true;
    }

    fDmaDesc = IOBufferMemoryDescriptor::withCapacity (R11_DMA_DESC_SIZE, kIODirectionOutIn, true);
    fDmaData = IOBufferMemoryDescriptor::withCapacity (R11_DMA_DATA_SIZE, kIODirectionOutIn, true);
    if (!fDmaDesc || !fDmaData) {
        IOLog ("NativeSdhci[" kSdhciTag "]: dma: buffer allocation failed (desc=%p data=%p)\n",
               fDmaDesc, fDmaData);
        return false;
    }

    IOByteCount descLen = 0;
    IOByteCount dataLen = 0;
/* getPhysicalSegment 在部分 SDK 里带 deprecated 标记；这里确实要用物理地址，
 * 而且它是最直接的接口，所以就地关掉这条告警（CI 用 -Werror）。 */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    fDmaDescPhys = fDmaDesc->getPhysicalSegment (0, &descLen, kIODirectionOut);
    fDmaDataPhys = fDmaData->getPhysicalSegment (0, &dataLen, kIODirectionOut);
#pragma clang diagnostic pop
    IOLog ("NativeSdhci[" kSdhciTag "]: dma: desc va=%p phys=%08llx len=%llu / data va=%p phys=%08llx len=%llu\n",
           fDmaDesc->getBytesNoCopy (), (unsigned long long) fDmaDescPhys,
           (unsigned long long) descLen, fDmaData->getBytesNoCopy (),
           (unsigned long long) fDmaDataPhys, (unsigned long long) dataLen);
    return fDmaDescPhys != 0 && fDmaDataPhys != 0;
}

bool NativeSdhci::beginDmaRead (UInt32 bytes)
{
    if (bytes > R11_DMA_DATA_SIZE || !setupDmaMemory ()) {
        return false;
    }

    uint32_t *desc = static_cast<uint32_t *> (fDmaDesc->getBytesNoCopy ());
    desc[0] = (bytes << 16) | 0x27u;                        /* Valid|End|Int|TRAN */
    desc[1] = (uint32_t) (fDmaDataPhys & 0xFFFFFFFFu);
    desc[2] = (uint32_t) ((uint64_t) fDmaDataPhys >> 32);
    desc[3] = 0;

    fDmaDesc->prepare (kIODirectionOut);
    fDmaData->prepare (kIODirectionIn);

    sdhciWrite32 (fCore, SDHCI_ADMA_ADDRESS, (uint32_t) (fDmaDescPhys & 0xFFFFFFFFu));
    IOLog ("NativeSdhci[" kSdhciTag "]: dma: armed bytes=%u desc0=%08x %08x %08x adma=%08x\n",
           bytes, desc[0], desc[1], desc[2], sdhciRead32 (fCore, SDHCI_ADMA_ADDRESS));
    return true;
}

bool NativeSdhci::endDmaRead (void *dst, UInt32 bytes)
{
    bool ok = waitTransferComplete ();

    uint32_t state = sdhciRead32 (fCore, SDHCI_PRESENT_STATE);
    uint32_t nis   = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
    uint16_t eis   = sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS);
    uint32_t ea0   = sdhciRead32 (fCore, CORE_ADMA_ERR_ADDR0);
    uint32_t ea1   = sdhciRead32 (fCore, CORE_ADMA_ERR_ADDR1);
    uint32_t *data = static_cast<uint32_t *> (fDmaData->getBytesNoCopy ());

    fDmaData->complete (kIODirectionIn);

    IOLog ("NativeSdhci[" kSdhciTag "]: dma: done ok=%u state=%08x nis=%08x eis=%04x adma=%08x err=%08x/%08x\n",
           ok ? 1 : 0, state, nis, eis, sdhciRead32 (fCore, SDHCI_ADMA_ADDRESS), ea0, ea1);
    /* 前四个字 + EXT_CSD[212] 所在的第 53 个字（SEC_COUNT），一眼看出数据是不是真的 */
    IOLog ("NativeSdhci[" kSdhciTag "]: dma: data %08x %08x %08x %08x | w52=%08x w53=%08x w126=%08x w127=%08x\n",
           data[0], data[1], data[2], data[3], data[52], data[53], data[126], data[127]);

    if (!ok || dst == nullptr) {
        return false;
    }
    memcpy (dst, data, bytes);
    return true;
}

bool NativeSdhci::readExtCsd ()
{
    uint8_t ext[kSdhciBlockSize] __attribute__ ((aligned (64)));

    for (uint32_t attempt = 0; attempt < 2; attempt++) {
        memset (ext, 0, sizeof (ext));

        sdhciWrite16 (fCore, SDHCI_BLOCK_SIZE, (uint16_t) kSdhciBlockSize);
        sdhciWrite16 (fCore, SDHCI_BLOCK_COUNT, 1);
        /* BLK_CNT_EN 让控制器按 BLOCK_COUNT 字节数收尾（单块也要置位，
         * 否则某些 MSM 版本会按"开到 CMD12 为止"处理，DATA_END 永远不来）。 */
        sdhciWrite16 (fCore, SDHCI_TRANSFER_MODE,
                      (uint16_t) (SDHCI_TRNS_DMA | SDHCI_TRNS_READ | SDHCI_TRNS_BLK_CNT_EN));

        IOLog ("NativeSdhci[" kSdhciTag "]: CMD8 setup blksz=%04x cnt=%04x mode=%04x word28=%08x attempt=%u\n",
               sdhciRead16 (fCore, SDHCI_BLOCK_SIZE), sdhciRead16 (fCore, SDHCI_BLOCK_COUNT),
               sdhciRead16 (fCore, SDHCI_TRANSFER_MODE), sdhciReadHostWord (fCore), attempt);

        bool ok = false;
        if (!beginDmaRead ((UInt32) kSdhciBlockSize)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD8 dma setup failed (attempt %u)\n", attempt);
        } else if (!sendCommand (MMC_SEND_EXT_CSD, 0,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX | SDHCI_CMD_DATA,
                          nullptr)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD8 (SEND_EXT_CSD) failed (attempt %u)\n", attempt);
        } else {
            /* 命令阶段成功的证据：DAT_LINE_ACTIVE 应该已起、ADMA_ADDRESS 应该是 0。
             * 注意 EIS(0x32) **只能 16 位读** —— 0x32 % 4 == 2，用 32 位读是错位访问，
             * Snapdragon 的 AHB 从口不会拆错位事务，会直接把总线挂死（v16 就是这样
             * "打完 cmd8 complete 就静默"：死在这一行的参数求值里）。 */
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD8 ok state=%08x nis=%08x eis=%08x adma=%08x\n",
                   sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
                   sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS),
                   sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS),
                   sdhciRead32 (fCore, SDHCI_ADMA_ADDRESS));
            if (endDmaRead (ext, (UInt32) kSdhciBlockSize)) {
                ok = true;
            } else {
                IOLog ("NativeSdhci[" kSdhciTag "]: EXT_CSD transfer failed (attempt %u)\n", attempt);
            }
        }

        if (!ok) {
            recoverCommandState ("after CMD8 data failure");
            continue;
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

    return false;
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
    sdhciDelayUs (10000);

    /* CMD1：SEND_OP_COND，直到卡退出 busy（HCS=1 表示支持 1.8V 高容量） */
    native_sdhci_step = 2;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD1 (op cond)\n");
    bool ready = false;
    uint32_t fails = 0;
    for (uint32_t i = 0; i < 300; i++) {
        /* CMD1 的响应是 R3：**没有 CRC**。带 SDHCI_CMD_CRC 会被当成 CRC 错；
         * 参数 0x40FF8000 = HCS(bit30, 支持高容量) + OCR 电压窗 3.2–3.4V。 */
        if (!sendCommand (MMC_SEND_OP_COND, 0x40FF8000U,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_INDEX, resp)) {
            fails++;
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD1 failed (%u) state=%08x norm=%04x err=%04x mstat=%08x\n",
                   fails, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
                   sdhciRead16 (fCore, SDHCI_NORMAL_INT_STATUS),
                   sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS),
                   sdhciRead32 (fCore, CORE_MCI_STATUS_REG));
            if (fails >= 3) {
                return false;
            }
            sdhciDelayUs (10000);
            continue;
        }
        if (i < 3 || (i % 100) == 0) {
            IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD1 resp[%u]=%08x state=%08x\n",
                   i, resp[0], sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
        }
        if (resp[0] & 0x80000000U) {
            ready = true;
            break;
        }
        sdhciDelayUs (10000);
    }
    if (!ready) {
        IOLog ("NativeSdhci[" kSdhciTag "]: card never left busy (CMD1), last=%08x\n", resp[0]);
        return false;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: OCR=%08x\n", resp[0]);

    /* CMD1 完成时带着幽灵 error 汇总位（v12 实测 word=00008001），清掉 NIS 之后
     * 控制器里可能还留着错误锁存，先复位一次命令状态机再发 CMD2。 */
    recoverCommandState ("after CMD1 (stale error latch)");

    /* CMD2：拿 CID（长响应）。这一步失败不致命 —— eMMC 的 RCA 由 host 指定，
     * CID 只是信息，真正必须的是 CMD3/CMD9/CMD7，所以失败也继续往下走，
     * 好让屏幕上看清到底是"136 位长响应这条路不通"还是"整条命令线不通"。 */
    /* CMD2：拿 CID（136 位长响应）。**不设 SDHCI_CMD_INDEX** ——
     * R2 的响应里没有命令索引字段，打开"Command Index Check"会让控制器
     * 永远等一个不会出现的索引（v13 实测：CMD2/CMD9 卡在 CMD_INHIBIT、
     * 既不完成也不报错，而 48 位 R1 的 CMD3/CMD7/CMD16 都正常）。 */
    native_sdhci_step = 3;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD2 (cid)\n");
    if (!sendCommand (MMC_ALL_SEND_CID, 0,
                      SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC, resp)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD2 failed, continuing without CID\n");
        native_sdhci_cid0 = 0;
        recoverCommandState ("after CMD2 failure");
    } else {
        native_sdhci_cid0 = resp[3];
    }

    /* CMD3：分配 RCA（eMMC 由 host 指定） */
    native_sdhci_step = 4;
    fRca = 0x0001;
    IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD3 (rca=1), cid0=%08x\n", native_sdhci_cid0);
    if (!sendCommand (MMC_SET_RELATIVE_ADDR, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD3 failed, retrying after recovery\n");
        recoverCommandState ("after CMD3 failure");
        if (!sendCommand (MMC_SET_RELATIVE_ADDR, (uint32_t) fRca << 16,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: cardInit: CMD3 failed twice\n");
            return false;
        }
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

    for (uint32_t attempt = 0; attempt < 2; attempt++) {
        sdhciWrite16 (fCore, SDHCI_BLOCK_SIZE, (uint16_t) kSdhciBlockSize);
        sdhciWrite16 (fCore, SDHCI_BLOCK_COUNT, 1);
        sdhciWrite16 (fCore, SDHCI_TRANSFER_MODE,
                      (uint16_t) (SDHCI_TRNS_DMA | SDHCI_TRNS_READ | SDHCI_TRNS_BLK_CNT_EN));

        if (!beginDmaRead ((UInt32) kSdhciBlockSize)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD17 dma setup failed (attempt %u)\n", attempt);
        } else if (!sendCommand (MMC_READ_SINGLE_BLOCK, (uint32_t) block,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX | SDHCI_CMD_DATA,
                          nullptr)) {
            IOLog ("NativeSdhci[" kSdhciTag "]: CMD17 lba=%llu failed (attempt %u)\n",
                   (unsigned long long) block, attempt);
        } else if (endDmaRead (buffer, (UInt32) kSdhciBlockSize)) {
            return kIOReturnSuccess;
        }

        recoverCommandState ("after CMD17 data failure");
    }
    return kIOReturnError;
}

IOReturn NativeSdhci::writeBlocks (UInt64 block, UInt32 count, const void *buffer)
{
    if (count != 1) {
        return kIOReturnUnsupported;
    }
    sdhciWrite16 (fCore, SDHCI_BLOCK_SIZE, (uint16_t) kSdhciBlockSize);
    sdhciWrite16 (fCore, SDHCI_BLOCK_COUNT, 1);
    sdhciWrite16 (fCore, SDHCI_TRANSFER_MODE, 0);

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
    if (!enableGccClocks ()) {
        /* 打不开根时钟就不要读 SDCC —— 那一步会让整机静默挂死，连 panic 都打不出来 */
        IOLog ("NativeSdhci[" kSdhciTag "]: gcc clocks not ready, giving up before touching SDCC\n");
        goto fail;
    }
    logGccState ();

    /* 第一次触 SDCC：先用 32 位、4 字节对齐的 PRESENT_STATE(0x24) 探活，
     * 再用 16 位读 HOST_VERSION(0xFE)。v3/v4 在这里用 32 位读 0xFE 是错位访问，
     * 总线直接不应答，所以连这行 log 都打不出来。 */
    IOLog ("NativeSdhci[" kSdhciTag "]: start: probing SDCC @0x24 (32-bit, aligned)\n");
    native_sdhci_state = sdhciRead32 (fCore, SDHCI_PRESENT_STATE);
    IOLog ("NativeSdhci[" kSdhciTag "]: state=%08x\n", native_sdhci_state);

    IOLog ("NativeSdhci[" kSdhciTag "]: start: probing SDCC @0xFE (16-bit)\n");
    native_sdhci_version = sdhciRead16 (fCore, SDHCI_HOST_VERSION) & 0xFF;
    native_sdhci_caps    = sdhciRead32 (fCore, SDHCI_CAPABILITIES);
    native_sdhci_caps1   = sdhciRead32 (fCore, SDHCI_CAPABILITIES_1);
    IOLog ("NativeSdhci[" kSdhciTag "]: ver=%02x caps=%08x caps1=%08x\n",
           native_sdhci_version, native_sdhci_caps, native_sdhci_caps1);

    /* 0x00 / 0xff 都说明控制器没应答（时钟关或地址错），继续下去没意义 */
    if (native_sdhci_version == 0x00 || native_sdhci_version == 0xff) {
        IOLog ("NativeSdhci[" kSdhciTag "]: controller not responding, giving up\n");
        goto fail;
    }

    /* 时间基准自检：v6 实测 IODelay(100) 不返回，v7 改用 CNTPCT_EL0 自旋。
     * 日志按 32 位两段打印，因为 IOLog 的 %llu 在本内核里输出不可信。
     * 单独起一个作用域：C++ 不允许 goto 跳过带初始化的局部变量。 */
    {
        uint32_t freq = (uint32_t) sdhciCounterFreq ();
        uint64_t startTicks = sdhciCounter ();
        IOLog ("NativeSdhci[" kSdhciTag "]: delay probe: begin (cntfrq=%u cntpct=%08x%08x)\n",
               freq, (uint32_t) (startTicks >> 32), (uint32_t) startTicks);
        sdhciDelayUs (100);
        uint64_t delta = sdhciCounter () - startTicks;
        IOLog ("NativeSdhci[" kSdhciTag "]: delay probe: done (advanced=%08x%08x)\n",
               (uint32_t) (delta >> 32), (uint32_t) delta);
    }

    /* 先把 vendor 侧的模式/IO 电平拨回"默认模式"（ABL 可能留着 HS400 配置） */
    configureVendorSpec ();

    /* 只复位 CMD/DATA 状态机：全复位会关掉卡时钟并且在本平台卡住（v7 实测） */
    IOLog ("NativeSdhci[" kSdhciTag "]: start: asserting software reset (CMD|DATA, 0x2C word)\n");
    if (!softReset (SDHCI_RESET_CMD | SDHCI_RESET_DATA, kSdhciCmdLoops)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: reset timed out\n");
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: reset done\n");
    if (!setBusPowerAndClock ()) {
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: clock done\n");
    if (!setupDmaMemory ()) {
        IOLog ("NativeSdhci[" kSdhciTag "]: dma memory not available, giving up\n");
        goto fail;
    }
    if (!cardInit ()) {
        IOLog ("NativeSdhci[" kSdhciTag "]: card init failed at step %u (state=%08x err=%04x)\n",
               native_sdhci_step, sdhciRead32 (fCore, SDHCI_PRESENT_STATE),
               sdhciRead16 (fCore, SDHCI_ERROR_INT_STATUS));
        goto fail;
    }
    IOLog ("NativeSdhci[" kSdhciTag "]: start: card init done\n");

    native_sdhci_step = 9;
    /* 先按 400 kHz 做介质探测：v13 实测在 19.2 MHz 下第一次数据读
     * （CMD8 SEND_EXT_CSD）会 data timeout，先用最低速把链路跑通，
     * 探测成功之后再升频。 */
    IOLog ("NativeSdhci[" kSdhciTag "]: start: probing media at 400 kHz\n");
    if (!probeMedia ()) {
        IOLog ("NativeSdhci[" kSdhciTag "]: media probe failed at step %u\n", native_sdhci_step);
        goto fail;
    }
    /* 探测通了再升频：19.2 MHz（M/N = 1/1），给后续块读取用 */
    if (!setBusClock (19200000)) {
        IOLog ("NativeSdhci[" kSdhciTag "]: could not raise the transfer clock, staying at 400 kHz\n");
        setBusClock (400000);
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
    if (fDmaDesc) {
        fDmaDesc->release ();
        fDmaDesc = nullptr;
    }
    if (fDmaData) {
        fDmaData->release ();
        fDmaData = nullptr;
    }
    fDmaDescPhys = 0;
    fDmaDataPhys = 0;

    fCoreMap.reset ();
    fVendorMap.reset ();
    fCore = 0;
    fVendor = 0;
    IOService::stop (provider);
}

void NativeSdhci::free ()
{
    if (fDmaDesc) {
        fDmaDesc->release ();
        fDmaDesc = nullptr;
    }
    if (fDmaData) {
        fDmaData->release ();
        fDmaData = nullptr;
    }
    fDmaDescPhys = 0;
    fDmaDataPhys = 0;

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

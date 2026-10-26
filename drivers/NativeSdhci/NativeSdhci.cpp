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
 * 状态：骨架 + 可用的 PIO 读路径；写路径、CMD6 高速切换、ADMA2、中断都是 TODO。
 */
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IOMemoryMap.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <IOKit/storage/IOBlockStorageDevice.h>
#include <IOKit/storage/IOStorage.h>
#include <pexpert/pexpert.h>
#include <string.h>
#include <string>

#include "SdhciRegs.h"

#define kSdhciBlockSize   512ULL
#define kSdhciCmdLoops    200000U     /* 轮询上限（约 200 ms，IODelay(1) = 1 us） */
#define kSdhciDataLoops   4000000U    /* 数据阶段上限（约 4 s） */

/* 诊断计数器，panic 里能看到走到哪一步 */
volatile uint32_t native_sdhci_probe_calls = 0;
volatile uint32_t native_sdhci_matched     = 0;
volatile uint32_t native_sdhci_step        = 0;
volatile uint32_t native_sdhci_caps        = 0;
volatile uint32_t native_sdhci_caps1       = 0;
volatile uint32_t native_sdhci_version     = 0;
volatile uint32_t native_sdhci_rca         = 0;
volatile uint32_t native_sdhci_cid0        = 0;

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
    keep = OSSharedPtr<IOMemoryMap> (memory->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
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

class NativeSdhciBlock;

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
    bool     softReset (uint32_t which, uint32_t loops);
    bool     setBusPowerAndClock ();
    bool     cardInit ();

    bool     sendCommand (uint32_t index, uint32_t arg, uint32_t flags, uint32_t *resp);
    bool     waitCommandComplete ();
    bool     waitTransferComplete ();
    bool     pioTransfer (bool read, void *buffer, UInt32 bytes);
    bool     readCsd ();

    uintptr_t fCore     = 0;
    uintptr_t fVendor   = 0;
    OSSharedPtr<IOMemoryMap> fCoreMap;
    OSSharedPtr<IOMemoryMap> fVendorMap;
    UInt16    fRca      = 0;
    UInt64    fBlockCount = 0;
    NativeSdhciBlock *fBlockDevice = nullptr;
};

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
    IOReturn reportPollRequirements (bool *pollRequired, bool *pollAgain) override;
    IOReturn reportRemovability (bool *isRemovable) override;
    IOReturn reportWriteProtection (bool *isWriteProtected) override;

    /* ---- 非虚辅助（基类没有这两个虚函数，不能写 override）---- */
    IOReturn doSyncReadWrite (IOMemoryDescriptor *buffer, UInt64 block, UInt64 nblks);
    IOReturn getDeviceType (UInt8 *deviceType);

private:
    NativeSdhci *fController = nullptr;
};

OSDefineMetaClassAndStructors (NativeSdhci, IOService)
OSDefineMetaClassAndStructors (NativeSdhciBlock, IOBlockStorageDevice)

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
        match = text && std::strstr (text, "qcom,sdhci-msm-v5");
    }

    if (!match) {
        return nullptr;
    }
    native_sdhci_matched = 1;
    if (score) {
        *score = 1000;
    }
    return this;
}

bool NativeSdhci::mapRegisters (IOService *provider)
{
    /* 优先用 provider 给的 IODeviceMemory（ADT 的 reg 段），失败则用板级常量 */
    bool ok = false;
    if (fCore == 0) {
        if (provider && provider->getDeviceMemoryCount () > 0) {
            if (IODeviceMemory *memory = provider->getDeviceMemoryWithIndex (0)) {
                IOPhysicalAddress pa = memory->getPhysicalAddress ();
                fCoreMap = OSSharedPtr<IOMemoryMap> (
                    memory->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
                if (fCoreMap) {
                    fCore = static_cast<uintptr_t> (fCoreMap->getVirtualAddress ());
                    ok = true;
#if DEBUG
                    IOLog ("NativeSdhci: core from provider reg0 @0x%llx\n", (unsigned long long) pa);
#endif
                }
            }
        }
    }
    if (!ok) {
        fCore = sdhciMapPhys (R11_SDHCI_CORE_PHYS, R11_SDHCI_REG_SIZE, fCoreMap);
    }
    if (!fCore) {
        return false;
    }
    /* msm v5 的第二段是独立的 vendor 窗口；ADT 里通常作为 reg1 出现 */
    if (provider && provider->getDeviceMemoryCount () > 1) {
        if (IODeviceMemory *memory = provider->getDeviceMemoryWithIndex (1)) {
            fVendorMap = OSSharedPtr<IOMemoryMap> (
                memory->map (kIOMapAnywhere | kIOMapInhibitCache), OSNoRetain);
            if (fVendorMap) {
                fVendor = static_cast<uintptr_t> (fVendorMap->getVirtualAddress ());
            }
        }
    }
    if (!fVendor) {
        fVendor = sdhciMapPhys (R11_SDHCI_VENDOR_PHYS, R11_SDHCI_REG_SIZE, fVendorMap);
    }
    return fVendor != 0;
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
    sdhciWrite32 (fCore, SDHCI_CLOCK_CONTROL, clk);
    if (!sdhciWaitSet (fCore, SDHCI_CLOCK_CONTROL, SDHCI_CLOCK_INT_STABLE, kSdhciCmdLoops)) {
        IOLog ("NativeSdhci: internal clock never stabilised\n");
        return false;
    }
    clk |= SDHCI_CLOCK_CARD_EN;
    sdhciWrite32 (fCore, SDHCI_CLOCK_CONTROL, clk);
    IODelay (1000);
    return true;
}

bool NativeSdhci::waitCommandComplete ()
{
    for (uint32_t i = 0; i < kSdhciCmdLoops; i++) {
        uint32_t status = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
        if (status & SDHCI_INT_ERROR) {
            uint32_t err = sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS);
            sdhciWrite32 (fCore, SDHCI_ERROR_INT_STATUS, err);
            IOLog ("NativeSdhci: cmd error int=%08x err=%08x\n", status, err);
            return false;
        }
        if (status & SDHCI_INT_RESPONSE) {
            sdhciWrite32 (fCore, SDHCI_NORMAL_INT_STATUS, SDHCI_INT_RESPONSE);
            return true;
        }
        IODelay (1);
    }
    IOLog ("NativeSdhci: cmd timeout state=%08x\n", sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
    return false;
}

bool NativeSdhci::waitTransferComplete ()
{
    for (uint32_t i = 0; i < kSdhciDataLoops; i++) {
        uint32_t status = sdhciRead32 (fCore, SDHCI_NORMAL_INT_STATUS);
        if (status & SDHCI_INT_ERROR) {
            uint32_t err = sdhciRead32 (fCore, SDHCI_ERROR_INT_STATUS);
            sdhciWrite32 (fCore, SDHCI_ERROR_INT_STATUS, err);
            IOLog ("NativeSdhci: data error int=%08x err=%08x\n", status, err);
            return false;
        }
        if (status & SDHCI_INT_DATA_END) {
            sdhciWrite32 (fCore, SDHCI_NORMAL_INT_STATUS, SDHCI_INT_DATA_END);
            return true;
        }
        IODelay (1);
    }
    IOLog ("NativeSdhci: data timeout state=%08x\n", sdhciRead32 (fCore, SDHCI_PRESENT_STATE));
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
                IOLog ("NativeSdhci: pio read starved at word %u\n", i);
                return false;
            }
            words[i] = sdhciRead32 (fCore, SDHCI_BUFFER);
        }
    } else {
        for (UInt32 i = 0; i < count; i++) {
            if (!sdhciWaitSet (fCore, SDHCI_PRESENT_STATE, SDHCI_SPACE_AVAILABLE, kSdhciDataLoops)) {
                IOLog ("NativeSdhci: pio write stalled at word %u\n", i);
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

bool NativeSdhci::cardInit ()
{
    uint32_t resp[4] = {0, 0, 0, 0};

    native_sdhci_step = 1;
    /* CMD0：回到 idle（此时 CMD 线上不需要 CRC） */
    if (!sendCommand (MMC_GO_IDLE_STATE, 0, SDHCI_CMD_RESP_NONE, nullptr)) {
        IOLog ("NativeSdhci: CMD0 failed\n");
        return false;
    }
    IOSleep (10);

    /* CMD1：SEND_OP_COND，直到卡退出 busy（HCS=1 表示支持 1.8V 高容量） */
    native_sdhci_step = 2;
    bool ready = false;
    for (uint32_t i = 0; i < 1000; i++) {
        if (!sendCommand (MMC_SEND_OP_COND, 0x40FF8080,
                          SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
            return false;
        }
        if (resp[0] & 0x80000000U) {
            ready = true;
            break;
        }
        IOSleep (10);
    }
    if (!ready) {
        IOLog ("NativeSdhci: card never left busy (CMD1)\n");
        return false;
    }

    /* CMD2：拿 CID（长响应），顺便当作"卡还在"的探针 */
    native_sdhci_step = 3;
    if (!sendCommand (MMC_ALL_SEND_CID, 0,
                      SDHCI_CMD_RESP_LONG | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }
    native_sdhci_cid0 = resp[3];

    /* CMD3：分配 RCA（eMMC 由 host 指定） */
    native_sdhci_step = 4;
    fRca = 0x0001;
    if (!sendCommand (MMC_SET_RELATIVE_ADDR, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_SHORT | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }
    native_sdhci_rca = fRca;

    /* CMD9：读 CSD，拿容量 */
    native_sdhci_step = 5;
    (void) readCsd ();

    /* CMD7：选中卡 */
    native_sdhci_step = 6;
    if (!sendCommand (MMC_SELECT_CARD, (uint32_t) fRca << 16,
                      SDHCI_CMD_RESP_SHORT_BUSY | SDHCI_CMD_CRC | SDHCI_CMD_INDEX, resp)) {
        return false;
    }

    /* CMD16：块长 512 */
    native_sdhci_step = 7;
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
    if (!IOService::start (provider)) {
        return false;
    }
    if (!mapRegisters (provider)) {
        IOLog ("NativeSdhci: register mapping failed\n");
        goto fail;
    }

    native_sdhci_version = sdhciRead32 (fCore, SDHCI_HOST_VERSION) & 0xFF;
    native_sdhci_caps    = sdhciRead32 (fCore, SDHCI_CAPABILITIES);
    native_sdhci_caps1   = sdhciRead32 (fCore, SDHCI_CAPABILITIES_1);

    if (!softReset (SDHCI_RESET_ALL, kSdhciCmdLoops)) {
        IOLog ("NativeSdhci: reset timed out\n");
        goto fail;
    }
    if (!setBusPowerAndClock ()) {
        goto fail;
    }
    if (!cardInit ()) {
        IOLog ("NativeSdhci: card init failed at step %u\n", native_sdhci_step);
        goto fail;
    }

    /* 读 block 0 做一次链路自检（MBR/GPT 头，失败不代表链路坏，只记录） */
    {
        uint8_t probe[kSdhciBlockSize] __attribute__ ((aligned (64)));
        if (readBlocks (0, 1, probe) == kIOReturnSuccess) {
            native_sdhci_step = 9;
        }
    }

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
    return true;

fail:
    return false;
}

void NativeSdhci::stop (IOService *provider)
{
    if (fBlockDevice) {
        fBlockDevice->terminate (kIOServiceSynchronous);
        fBlockDevice->release ();
        fBlockDevice = nullptr;
    }
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

/* ========================= NativeSdhciBlock ========================= */

bool NativeSdhciBlock::init (NativeSdhci *controller)
{
    if (!IOBlockStorageDevice::init () || !controller) {
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
    IOLog ("NativeSdhci: block device online, %llu blocks\n",
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
            buffer->writeBytes (0, scratch, kSdhciBlockSize);
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

IOReturn NativeSdhciBlock::getDeviceType (UInt8 *deviceType)
{
    if (!deviceType) {
        return kIOReturnBadArgument;
    }
    *deviceType = kIOBlockStorageDeviceTypeGeneric;
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

IOReturn NativeSdhciBlock::reportPollRequirements (bool *pollRequired, bool *pollAgain)
{
    if (!pollRequired || !pollAgain) {
        return kIOReturnBadArgument;
    }
    *pollRequired = false;   /* eMMC 不会自己拔出，不需要轮询 */
    *pollAgain    = false;
    return kIOReturnSuccess;
}

/*
 * R11 bring-up stubs.
 *
 * NativeRamBlock 的持久化层（persist::Overlay）会引用 _native_ufs_store_*，
 * 而实现在 NativeUfs 里，它又依赖 VerifiedRoot 资格化体系
 * （_vr_open / _vr_verify_block / ...）。R11 第一阶段只从 ramdisk 读，
 * 不会把数据写回 UFS，所以这里给返回错误的桩：链接能过，持久化路径直接失败。
 *
 * 另外 bsd_init 会调用 _native_legacy_display_probe 做显示探测；
 * 桩返回 0 表示没有 legacy 显示设备，内核就不做 legacy 显示初始化。
 *
 * 注意：需要的符号本身以 "_" 开头（C++ 侧写的是 extern "C" 的 _native_*），
 * 而 C 函数名会自动再加一个 "_" 前缀，所以用 __asm__ 标签钉死符号名。
 */
typedef unsigned long long u64;

int r11_store_open(void *device, u64 capacity_bytes, u64 block_size)
    __asm__("_native_ufs_store_open");
int r11_store_read(u64 lba, void *buffer, u64 bytes)
    __asm__("_native_ufs_store_read");
int r11_store_write(u64 lba, const void *buffer, u64 bytes)
    __asm__("_native_ufs_store_write");
int r11_store_sync(void)
    __asm__("_native_ufs_store_sync");

/* NativeRamBlock::attachStore() 还会引用这两个符号。
 * 它们同样属于 NativeUfs 的持久化层（依赖 VerifiedRoot），bring-up 阶段返回 -1，
 * 表示"没有可用的 GPT 校验 / 模式查询"，驱动会走内存盘的只读路径。 */
int r11_store_gpt_check(void)
    __asm__("_native_ufs_store_gpt_check");
int r11_store_mode(void)
    __asm__("_native_ufs_store_mode");


int r11_legacy_display_probe(void)
    __asm__("_native_legacy_display_probe");
int r11_legacy_display_late_probe(void)
    __asm__("_native_legacy_display_late_probe");

int r11_store_open(void *device, u64 capacity_bytes, u64 block_size)
{
  (void)device;
  (void)capacity_bytes;
  (void)block_size;
  return -1;
}

int r11_store_read(u64 lba, void *buffer, u64 bytes)
{
  (void)lba;
  (void)buffer;
  (void)bytes;
  return -1;
}

int r11_store_write(u64 lba, const void *buffer, u64 bytes)
{
  (void)lba;
  (void)buffer;
  (void)bytes;
  return -1;
}

int r11_store_sync(void)
{
  return -1;
}

int r11_store_gpt_check(void)
{
  return -1;
}

int r11_store_mode(void)
{
  return -1;
}

int r11_legacy_display_probe(void)
{
  return 0;
}

int r11_legacy_display_late_probe(void)
{
  return 0;
}

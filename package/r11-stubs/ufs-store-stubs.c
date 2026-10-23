/*
 * R11 bring-up 桩：NativeRamBlock 的持久化层（persist::Overlay）会引用
 * _native_ufs_store_open/read/write/sync，而这套实现在 NativeUfs 里，
 * 它又依赖 VerifiedRoot 资格化体系（_vr_open / _vr_verify_block / ...）。
 *
 * R11 第一阶段只从 ramdisk（seed / 稀疏 RAM 盘）读，不会真的把数据写回 UFS。
 * 所以这里给出返回错误的桩：链接能过，运行时持久化路径直接失败（不写盘）。
 *
 * 注意：需要的符号本身以 "_" 开头（C++ 里写的是 extern "C" 的 _native_ufs_store_*），
 * 而 C 函数名会自动加一个 "_" 前缀，所以这里用 __asm__ 标签钉死符号名。
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

int r11_store_open(void *device, u64 capacity_bytes, u64 block_size)
{
  (void)device; (void)capacity_bytes; (void)block_size;
  return -1;
}

int r11_store_read(u64 lba, void *buffer, u64 bytes)
{
  (void)lba; (void)buffer; (void)bytes;
  return -1;
}

int r11_store_write(u64 lba, const void *buffer, u64 bytes)
{
  (void)lba; (void)buffer; (void)bytes;
  return -1;
}

int r11_store_sync(void)
{
  return -1;
}

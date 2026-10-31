#!/usr/bin/env python3
"""把 exec_mach_imgact 里「load_machfile 返回非零就失败」这一道判断强行越过，
但**同时把原始返回值打印出来**。

用途：一次有边界的诊断实验（m08166 方案的 B）。
    - 如果强制走成功分支后 sugid / stack / copyout / svc 探针开始出数，
      说明这次失败只是返回值被污染，装载其实到了可用状态；
    - 如果立刻 panic，说明装载确实没完成，必须回去解决装载本身。

不猜地址：三个关键位置都做「地址 + 原始字节」双校验，任何一条不符立刻退出。
   0x76d4b58  cbz  w0, 0x76d4bb0   (0x340002c0)
   0x76d4b5c  mov  x24, x0         (0xaa0003f8)   仅作旁证
   0x76d4bb0  mov  x0, x20         (0xaa1403e0)   成功分支入口

跳板做的事（顺序不能变）：
   stp 保存 → mov w1,w0 → kprintf("R11: FORCE-EXEC real lret=%x") → 恢复 →
   mov w0,#0 → b 0x76d4bb0
"""

import argparse
import hashlib
import json
import struct
import subprocess
import sys

# ---- 常量（每个都在运行期做字节校验）----
CBZ_VA = 0xFFFFFFF0076D4B58
CBZ_WORD = 0x340002C0
MOVX24_VA = 0xFFFFFFF0076D4B5C
MOVX24_WORD = 0xAA0003F8
SUCCESS_VA = 0xFFFFFFF0076D4BB0
SUCCESS_WORD = 0xAA1403E0
MAGIC = b"R11FORCE"

TEXT_EXEC_VM = 0xFFFFFFF00723C000
TEXT_EXEC_FO = 0x238000
TEXT_EXEC_SIZE = 0x7F8000

BASE_SHA = "7312d043316ed7412928746ca1425f30f493cac020227d577e1e6e30341d4542"


def u32(d, fo):
    return struct.unpack("<I", d[fo:fo + 4])[0]


def enc_stp_pre(rt, rt2, rn, off):
    return 0xA9800000 | (((off // 8) & 0x7F) << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_ldp_off(rt, rt2, rn, off):
    return 0xA9400000 | (((off // 8) & 0x7F) << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_ldp_post(rt, rt2, rn, off):
    """ldp rt, rt2, [rn], #off —— post-index，弹出后 rn += off。"""
    return 0xA8C00000 | (((off // 8) & 0x7F) << 15) | (rt2 << 10) | (rn << 5) | rt


def enc_adrp(pc, target, rd):
    delta = target - pc
    immlo = (delta >> 12) & 0x3
    immhi = (delta >> 14) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | rd


def enc_add_xd_imm(rd, rn, imm12):
    return 0x91000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rd


def enc_bl(pc, target):
    return 0x94000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def enc_b(pc, target):
    return 0x14000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def _scan_cave(d, start, end, need):
    """从 start 开始找一段全零、向后至少 need 字节、且 16 字节对齐的空间。

    额外要求「页内偏移 <= 0xD00」，这样 cave 与 cave+0x100（格式串）一定同页，
    adrp + add 的立即数不会跨页。
    """
    i = start
    while i < end:
        if d[i] == 0:
            j = i
            while j < i + need and d[j] == 0:
                j += 1
            if j - i >= need:
                if i % 16 == 0 and (i & 0xFFF) <= 0xD00:
                    return TEXT_EXEC_VM + (i - TEXT_EXEC_FO)
                # 这一段够长但起点不满足约束：对齐后继续往里找
                i = i + 16
                continue
            i = j
        else:
            i += 4
    return None


def find_cave(d, need):
    """优先用 0xfffffff007a31000 之后那片已知大空白（trace 探针占前 4 KiB），
    避免 FORCE-EXEC 的跳板与探针打架；找不到再退回整段扫描。"""
    end = TEXT_EXEC_FO + TEXT_EXEC_SIZE - need
    preferred = 0xFFFFFFF007A31000
    if TEXT_EXEC_VM <= preferred < TEXT_EXEC_VM + TEXT_EXEC_SIZE - need:
        got = _scan_cave(d, TEXT_EXEC_FO + (preferred - TEXT_EXEC_VM), end, need)
        if got is not None:
            return got
    return _scan_cave(d, TEXT_EXEC_FO, end, need)


def disasm(path, va, n, label):
    d = open(path, "rb").read()
    fo = TEXT_EXEC_FO + (va - TEXT_EXEC_VM)
    buf = d[fo:fo + n]
    open("/tmp/_fe.bin", "wb").write(buf)
    out = subprocess.run(
        ["aarch64-linux-gnu-objdump", "-D", "-b", "binary", "-m", "aarch64",
         "--adjust-vma=%#x" % va, "/tmp/_fe.bin"],
        capture_output=True, text=True).stdout
    print("  --- %s @ %#x ---" % (label, va))
    for line in out.splitlines():
        if line.strip().startswith("ffff"):
            print("  " + line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kernel")
    ap.add_argument("output")
    ap.add_argument("--expect-sha", default=BASE_SHA)
    ap.add_argument("--manifest", default="/tmp/r11-force-exec.json")
    a = ap.parse_args()

    d = bytearray(open(a.kernel, "rb").read())
    sha_in = hashlib.sha256(d).hexdigest()

    print("== 输入 %s" % a.kernel)
    print("   sha256 %s" % sha_in)
    if a.expect_sha and sha_in != a.expect_sha:
        sys.exit("!! 输入内核不是预期基准（期望 %s）" % a.expect_sha)
    if MAGIC in d:
        sys.exit("!! 这份内核已经打过 FORCE-EXEC，先换干净内核")
    if b"R11TRACE" in d:
        print("   注意：内核里已有 R11TRACE 探针，这是允许的（探针与本次补丁互不干扰）")

    # ---- 1. 三个关键位置的字节校验 ----
    fo = TEXT_EXEC_FO + (CBZ_VA - TEXT_EXEC_VM)
    got = u32(d, fo)
    if got != CBZ_WORD:
        sys.exit("!! %#x 期望 %08x，实际 %08x" % (CBZ_VA, CBZ_WORD, got))
    got2 = u32(d, TEXT_EXEC_FO + (MOVX24_VA - TEXT_EXEC_VM))
    if got2 != MOVX24_WORD:
        sys.exit("!! %#x 期望 %08x，实际 %08x" % (MOVX24_VA, MOVX24_WORD, got2))
    got3 = u32(d, TEXT_EXEC_FO + (SUCCESS_VA - TEXT_EXEC_VM))
    if got3 != SUCCESS_WORD:
        sys.exit("!! %#x 期望 %08x，实际 %08x" % (SUCCESS_VA, SUCCESS_WORD, got3))
    print("== 1. 三处原始字节全部匹配（cbz / mov x24,x0 / mov x0,x20）")

    # ---- 2. 找空穴 ----
    cave = find_cave(d, 0x180)
    if cave is None:
        sys.exit("!! __TEXT_EXEC 里找不到 0x180 字节的空穴")
    print("== 2. 空穴 %#x" % cave)

    # ---- 3. 拼跳板 ----
    words = []
    pc = cave

    def emit(w):
        nonlocal pc
        words.append((pc, w))
        pc += 4

    emit(enc_stp_pre(29, 30, 31, -16))
    emit(enc_stp_pre(0, 1, 31, -16))
    emit(enc_stp_pre(2, 3, 31, -16))
    emit(0x2A0003E1)                       # mov w1, w0
    emit(enc_adrp(pc, cave + 0x100, 0))    # adrp x0, <fmt>
    emit(enc_add_xd_imm(0, 0, 0x100))      # add  x0, x0, #0x100
    emit(enc_bl(pc, 0xFFFFFFF0079462B4))   # bl _kprintf
    emit(enc_ldp_off(2, 3, 31, 0))
    emit(enc_ldp_off(0, 1, 31, 16))
    # 最后一条用 post-index，把 sp 一次性弹回原值（48 字节 = 3 对）
    emit(enc_ldp_post(29, 30, 31, 48))
    emit(0x52800000)                       # mov w0, #0
    emit(enc_b(pc, SUCCESS_VA))            # b 0x76d4bb0

    fmt_va = cave + 0x100
    fmt = b"R11: FORCE-EXEC real lret=%x\n\0"

    # adrp/add 用的是「页 + 页内偏移」，确认 fmt 在同一 4K 页里
    if (cave & ~0xFFF) != (fmt_va & ~0xFFF) or (fmt_va & 0xFFF) > 0xE00:
        sys.exit("!! 格式串与跳板不在同一页，adrp/add 需要重算")

    for va, w in words:
        o = TEXT_EXEC_FO + (va - TEXT_EXEC_VM)
        d[o:o + 4] = struct.pack("<I", w)
    o = TEXT_EXEC_FO + (fmt_va - TEXT_EXEC_VM)
    d[o:o + len(fmt)] = fmt
    # 魔数放在格式串后面，便于"重复打补丁检测"
    o2 = TEXT_EXEC_FO + (fmt_va - TEXT_EXEC_VM) + 0x40
    d[o2:o2 + len(MAGIC)] = MAGIC

    # ---- 4. 把 cbz 换成 b <tramp> ----
    d[fo:fo + 4] = struct.pack("<I", enc_b(CBZ_VA, cave))
    print("== 3. %#x: cbz w0 → b %#x" % (CBZ_VA, cave))

    open(a.output, "wb").write(d)
    sha_out = hashlib.sha256(d).hexdigest()
    print("== 输出 %s" % a.output)
    print("   sha256 %s（输入 %s）" % (sha_out, sha_in))

    # ---- 5. 前后反汇编 ----
    disasm(a.kernel, CBZ_VA - 4, 16, "补丁前")
    disasm(a.output, CBZ_VA - 4, 16, "补丁后")
    disasm(a.output, cave, 4 * 12 + 4, "跳板")

    man = {
        "script": "patch-kernel-force-exec.py",
        "kernel": a.kernel,
        "output": a.output,
        "input_sha256": sha_in,
        "output_sha256": sha_out,
        "patch_site": CBZ_VA,
        "patch_site_orig": CBZ_WORD,
        "success_target": SUCCESS_VA,
        "success_target_word": SUCCESS_WORD,
        "trampoline": cave,
        "fmt": fmt_va,
        "calls": {"kprintf": 0xFFFFFFF0079462B4},
        "note": "把 load_machfile 的非零返回值强制当作成功，同时打印原始值",
    }
    open(a.manifest, "w").write(json.dumps(man, indent=2))
    print("   manifest %s" % a.manifest)


if __name__ == "__main__":
    main()

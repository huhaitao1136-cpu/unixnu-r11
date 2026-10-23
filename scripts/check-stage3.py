#!/usr/bin/env python3
"""check-stage3.py — 检查阶段 3（编译 XNU 内核 + 打包 payload）的就绪情况。

在 Windows / WSL / macOS 都能跑；它不执行构建，只报告缺什么。

    python3 scripts/check-stage3.py
"""
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # unixnu-r11

# Windows 控制台默认 GBK，emoji 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
TREE_SCRIPTS = Path(r"E:\DeepSeekWorkspace\src\UniXNU\scripts")

ok, warn, bad = [], [], []


def check(cond, good, problem):
    (ok if cond else bad).append(good if cond else problem)


def main():
    board = tomllib.loads((ROOT / "board.toml").read_text(encoding="utf-8"))

    # 1) 主机工具
    print("== 主机工具 ==")
    for tool, why in (("xcodebuild", "编译 XNU（iphoneos SDK）"),
                      ("clang", "编译内核与驱动"),
                      ("cmake", "构建依赖"),
                      ("ninja", "构建依赖"),
                      ("unifdef", "内核头文件处理")):
        path = shutil.which(tool)
        status = f"✅ {path}" if path else "❌ 未找到"
        print(f"  {tool:12} {status:40} {why}")
        if path:
            ok.append(tool)
        else:
            bad.append(f"{tool}（{why}）")

    # 2) board.toml 关键字段
    print("\n== board.toml ==")
    ipsw = board.get("ipsw", {})
    payload = board.get("payload", {})
    adt = payload.get("adt", {})
    checks = {
        "ipsw.product": ipsw.get("product"),
        "ipsw.version": ipsw.get("version"),
        "ipsw.chip_id": ipsw.get("chip_id"),
        "payload.dram_size": payload.get("dram_size"),
        "payload.kernel_args": len(payload.get("kernel_args", [])),
        "payload.text_fixups": len(payload.get("text_fixups", [])),
        "payload.adt.tool": adt.get("tool"),
        "payload.adt.chip_id": adt.get("chip_id"),
        "payload.adt.cpu_regs": adt.get("cpu_regs"),
        "payload.adt.owned_memory_size": adt.get("owned_memory_size"),
    }
    for k, v in checks.items():
        print(f"  {k:32} = {v}")
        if v in (None, 0, "", []):
            bad.append(f"board.toml 缺 {k}")

    # 3) 设备仓库里的工具脚本
    print("\n== 设备仓库脚本 ==")
    for rel in ("scripts/make-r11-adt.py", "scripts/seed-data-skip-setup.py",
                "scripts/check-r11.py"):
        exists = (ROOT / rel).is_file()
        print(f"  {rel:34} {'✅' if exists else '❌ 缺失'}")
        if not exists:
            bad.append(rel)

    # 4) UniXNU 上游脚本（payload 步骤会调用）
    print("\n== UniXNU 上游脚本 ==")
    for name in ("seed-data-skip-setup.py", "patch-xnu-entry-probe.py",
                 "prepare-xnu-source.py"):
        p = TREE_SCRIPTS / name
        print(f"  {name:34} {'✅' if p.is_file() else f'❌ 缺（{p}）'}")
        if not p.is_file():
            warn.append(name)

    # 5) 已有的内核产物
    print("\n== 已有产物 ==")
    kernel = ROOT / "out" / "r11" / "kernel"
    for name in ("kernel.macho", "kernel.macho.adt", "payload.img"):
        p = kernel / name
        print(f"  {name:20} {'✅ ' + str(p.stat().st_size) + ' B' if p.is_file() else '— 还没有'}")

    print("\n================ 结论 ================")
    if bad:
        print(f"阻塞项 {len(bad)} 个：")
        for b in bad:
            print("  ❌", b)
    if warn:
        print(f"警告 {len(warn)} 个：")
        for w in warn:
            print("  ⚠️", w)
    if not bad:
        print("✅ 没有阻塞项，可以在 macOS 上跑：")
        print("   unixnu build -D <device> kernel package")
        print("   unixnu build -D <device> payload --kernel ... --trust-cache ...")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

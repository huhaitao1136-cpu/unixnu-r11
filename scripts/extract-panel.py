#!/usr/bin/env python3
"""extract-panel.py — 从 stock DTB 里抠出指定 panel 节点的全部属性，并解码 MIPI DSI 初始化序列。

用途：UEFI 的 DisplayDxe（Mu-Silicium）需要 panel 的 DSI 参数 + on-command 表；
ADT 的 display-config 需要分辨率/时序。本脚本把这些从设备树里直接导出成结构化文件。

用法：
    python extract-panel.py R11-backup/dt/dtb-05.dtb --match oppo16051 -o inputs/panel
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

FDT_MAGIC = 0xD00DFEED


class FDT:
    def __init__(self, blob: bytes):
        (magic, self.total, off_struct, off_strings, _rsv,
         self.version, _lc, _cpu, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
        if magic != FDT_MAGIC:
            raise SystemExit("bad FDT magic")
        self.strings = blob[off_strings:off_strings + size_strings]
        self.struct = blob[off_struct:off_struct + size_struct]

    def walk(self):
        i = 0
        depth = 0
        stack: list[str] = []
        while i + 4 <= len(self.struct):
            tok = struct.unpack_from(">I", self.struct, i)[0]; i += 4
            if tok == 1:
                end = self.struct.find(b"\0", i)
                if end < 0:
                    return
                name = self.struct[i:end].decode(errors="replace")
                i = (end + 4) & ~3
                stack.append(name or "/")
                yield depth, stack[-1], None, stack[:]
                depth += 1
            elif tok == 2:
                depth -= 1
                if stack:
                    stack.pop()
                if depth < 0:
                    return
            elif tok == 3:
                ln, nameoff = struct.unpack_from(">II", self.struct, i); i += 8
                val = self.struct[i:i + ln]
                i = (i + ln + 3) & ~3
                e = self.strings.find(b"\0", nameoff)
                yield depth - 1, None, (self.strings[nameoff:e].decode(errors="replace"), val), stack[:]
            elif tok == 4:
                continue
            elif tok == 9:
                return


def as_str(val: bytes) -> str | None:
    if not val:
        return None
    body = val.rstrip(b"\0")
    if body and all(32 <= b < 127 for b in body):
        return body.decode()
    return None


def as_u32s(val: bytes) -> list[int] | None:
    if len(val) % 4 or not val:
        return None
    return list(struct.unpack(f">{len(val)//4}I", val))


def decode_dsi_cmds(blob: bytes) -> list[dict]:
    """QCOM 的 qcom,mdss-dsi-*-command 格式：
       [type, last, vc, ack, wait, len(u16 BE), payload...] 循环。"""
    out = []
    i = 0
    while i + 7 <= len(blob):
        typ, last, vc, ack, wait = blob[i:i + 5]
        ln = struct.unpack_from(">H", blob, i + 5)[0]
        payload = blob[i + 7:i + 7 + ln]
        if len(payload) != ln:
            break
        out.append({
            "type": typ,              # 0x05 DCS short write, 0x15 DCS short write param,
            "last": last,             # 0x01 = 本条之后发 EOT
            "vc": vc, "ack": ack, "wait": wait,
            "len": ln,
            "data": [f"0x{b:02x}" for b in payload],
        })
        i += 7 + ln
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dtb")
    ap.add_argument("--match", required=True, help="节点名/兼容属性里的关键字，如 oppo16051")
    ap.add_argument("-o", "--outdir", default="inputs/panel")
    args = ap.parse_args()

    t = FDT(Path(args.dtb).read_bytes())
    target_path = None
    nodes: dict[tuple, list] = {}
    cur: list = []
    depth0 = None
    for depth, name, prop, path in t.walk():
        if name is not None:
            cur = []
            if args.match.lower() in name.lower():
                target_path = tuple(path)
                depth0 = depth
                nodes[target_path] = cur
        elif prop and depth0 is not None and target_path is not None:
            # 只收命中的那个节点本身（以及与它同级的兄弟不算）
            if tuple(path) == target_path:
                cur.append((prop[0], prop[1]))
                nodes[target_path] = cur

    if not nodes:
        raise SystemExit(f"no node matching {args.match!r}")

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    for path, props in nodes.items():
        name = "/".join(path)
        info: dict = {"path": name, "string": {}, "u32": {}, "commands": {}, "raw": {}}
        for pname, val in props:
            s = as_str(val)
            if s is not None:
                info["string"][pname] = s
                continue
            if pname.endswith("-command") or pname.endswith("-commands"):
                cmds = decode_dsi_cmds(val)
                if cmds and sum(c["len"] for c in cmds) + 7 * len(cmds) == len(val):
                    info["commands"][pname] = cmds
                    continue
            u = as_u32s(val)
            if u and len(u) <= 32:
                info["u32"][pname] = [f"0x{x:x}" for x in u]
                continue
            info["raw"][pname] = val.hex()

        safe = args.match.replace("/", "_")
        (outdir / f"panel-{safe}.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")

        print(f"=== {name} ===")
        for k, v in info["string"].items():
            print(f"  {k:42} {v[:90]}")
        for k, v in info["u32"].items():
            print(f"  {k:42} {' '.join(v[:10])}")
        for k, cmds in info["commands"].items():
            print(f"  {k:42} {len(cmds)} packet(s), {sum(c['len'] for c in cmds)} payload bytes")
            for c in cmds[:12]:
                print(f"      type=0x{c['type']:02x} last={c['last']} wait={c['wait']} "
                      f"len={c['len']}  {' '.join(c['data'])}")
            if len(cmds) > 12:
                print(f"      ... {len(cmds) - 12} more (see json)")
        print(f"\n  json -> {outdir / f'panel-{safe}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

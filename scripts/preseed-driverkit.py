#!/usr/bin/env python3
"""预生成 prepare-xnu-source.py 需要的 DriverKit 头文件缓存，避开 GitHub API 限流。

用法：
    python3 scripts/preseed-driverkit.py <tree 目录>

GitHub Actions 的 runner 是共享 IP，api.github.com 经常返回 403 rate limit，
导致 XNU 构建第一步就失败。prepare-xnu-source.py 只需要三个目录的文件清单
（JSON），我们用 git 稀疏克隆把文件取到本地，自己算 git blob sha1，
并把 download_url 指向 file:// —— restore() 用 urllib 读本地文件，校验一致。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

TAG = os.environ.get("UNIXNU_DRIVERKIT_TAG", "xnu-10063.121.3")
TRUST_TAG = os.environ.get("UNIXNU_TRUSTCACHE_TAG", "xnu-8796.101.5")
REPO = "https://github.com/apple-oss-distributions/xnu.git"

DIRS = [("iokit/DriverKit", TAG), ("iokit/DriverKit/crypto", TAG),
        ("EXTERNAL_HEADERS/TrustCache", TRUST_TAG)]


def clone_sparse(tag: str, paths: list, work: Path) -> Path:
    dest = work / tag
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             "--branch", tag, REPO, str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", *paths],
                   check=True)
    return dest


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def entries_for(root: Path, rel_dir: str) -> list:
    d = root / rel_dir
    out = []
    if not d.is_dir():
        print(f"  !! 目录不存在: {d}")
        return out
    for f in sorted(d.iterdir()):
        if not f.is_file() or not f.name.endswith(".h"):
            continue
        out.append({
            "name": f.name,
            "type": "file",
            "download_url": f.as_uri(),
            "sha": blob_sha(f.read_bytes()),
        })
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tree = Path(sys.argv[1]).resolve()
    cache = tree / ".cache" / "sources"
    work = Path(os.environ.get("UNIXNU_PRESEED_WORK", "/tmp/xnu-sparse"))
    cache.mkdir(parents=True, exist_ok=True)

    by_tag = {}
    for rel, tag in DIRS:
        by_tag.setdefault(tag, []).append(rel)

    roots = {}
    for tag, paths in by_tag.items():
        print(f"稀疏克隆 {tag}: {paths}")
        roots[tag] = clone_sparse(tag, paths, work)

    plan = [
        ("xnu-driverkit-{0}.json".format(TAG.removeprefix("xnu-")),
         roots[TAG], "iokit/DriverKit"),
        ("xnu-driverkit-crypto-{0}.json".format(TAG.removeprefix("xnu-")),
         roots[TAG], "iokit/DriverKit/crypto"),
        ("xnu-trustcache-{0}.json".format(TRUST_TAG.removeprefix("xnu-")),
         roots[TRUST_TAG], "EXTERNAL_HEADERS/TrustCache"),
    ]
    for name, root, rel in plan:
        entries = entries_for(root, rel)
        (cache / name).write_text(json.dumps(entries, indent=1))
        print(f"  {name}: {len(entries)} 个文件")
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

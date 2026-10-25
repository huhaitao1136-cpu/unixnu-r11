#!/usr/bin/env python3
"""从远程 IPSW（ZIP）里按需取出单个成员，不用下载整个几 GB 的包。

原理：ZIP 的中央目录在文件尾部。先 range-fetch 尾部找到目录，
再 range-fetch 目标成员的数据段并解压。带 1 MiB 块缓存，避免重复请求。

用法:
  python3 fetch-ipsw-member.py <URL> --list                 # 列出所有 .dmg / 成员
  python3 fetch-ipsw-member.py <URL> --list --filter dmg    # 只列名字含 dmg 的
  python3 fetch-ipsw-member.py <URL> --extract RestoreRamDisk --out /tmp/rrd.dmg
"""
import argparse
import io
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

BLOCK = 1 << 20  # 1 MiB


class HttpRangeFile(io.RawIOBase):
    """只读、按块缓存的 HTTP Range 文件对象，供 zipfile 使用。"""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req) as r:
            self.size = int(r.headers['Content-Length'])
        self.pos = 0
        self._cache = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _block(self, index: int) -> bytes:
        if index in self._cache:
            return self._cache[index]
        start = index * BLOCK
        end = min(start + BLOCK - 1, self.size - 1)
        req = urllib.request.Request(self.url, headers={'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(req) as r:
            data = r.read()
        self._cache[index] = data
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        return data

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.pos
        out = bytearray()
        while n > 0:
            idx = self.pos // BLOCK
            off = self.pos % BLOCK
            chunk = self._block(idx)
            if off >= len(chunk):
                break
            take = min(n, len(chunk) - off)
            out += chunk[off:off + take]
            self.pos += take
            n -= take
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--filter', default=None)
    ap.add_argument('--extract', default=None)
    ap.add_argument('--out', type=Path, default=None)
    a = ap.parse_args()

    print(f'打开 {a.url}', flush=True)
    rf = HttpRangeFile(a.url)
    print(f'大小 {rf.size / (1 << 30):.2f} GiB', flush=True)
    zf = zipfile.ZipFile(rf)

    infos = zf.infolist()
    print(f'成员数 {len(infos)}')

    if a.list or a.extract:
        hits = [i for i in infos if (a.filter is None or a.filter.lower() in i.filename.lower())]
        if a.extract:
            hits = [i for i in hits if a.extract.lower() in i.filename.lower()]
        hits.sort(key=lambda i: -i.file_size)
        for i in hits[:40]:
            print(f'  {i.filename}  {i.file_size / (1 << 20):.1f} MiB  compressed {i.compress_size / (1 << 20):.1f} MiB')

    if a.extract:
        if not a.out:
            print('!! --extract 需要 --out')
            return 1
        target = None
        for i in infos:
            if a.extract.lower() in i.filename.lower():
                target = i
                break
        if target is None:
            print('!! 没找到匹配成员')
            return 1
        print(f'提取 {target.filename} ({target.file_size / (1 << 20):.1f} MiB) → {a.out}', flush=True)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(target) as src, a.out.open('wb') as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        print(f'完成: {a.out} ({a.out.stat().st_size} B)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""提取经过固定哈希校验的官方 3proxy 程序，不安装 deb 包、不运行安装脚本。"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tarfile
import urllib.request

RELEASES = {
    'arm64': ('dc2f217251d1f25514b69aa8310cebd89a9070950745326b05ac7908f4fd7a41', 183),
    'x86_64': ('a27525c24a7240895d5de8d5fa6b7489638a2b98aa1cee81cf1030163ab3d92b', 62),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='尚不存在的输出目录')
    parser.add_argument('--archive', help='可选的本地官方 deb 包')
    parser.add_argument('--arch', choices=RELEASES)
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('在 Linux 上准备程序')
    arch = args.arch or {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64', 'amd64': 'x86_64'}.get(platform.machine())
    if arch not in RELEASES:
        parser.error('此架构请使用 build_3proxy.py 从源码构建')
    dest = Path(args.output).resolve()
    if dest.exists():
        parser.error('输出目录已存在，拒绝覆盖')
    filename = '3proxy-1.0.0.' + arch + '.deb'
    url = 'https://github.com/3proxy/3proxy/releases/download/1.0.0/' + filename
    if args.archive:
        payload = Path(args.archive).read_bytes()
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'warp-pool-next-setup'})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(4 * 1024 * 1024)
    expected, machine = RELEASES[arch]
    if hashlib.sha256(payload).hexdigest() != expected:
        parser.error('官方程序包 SHA256 不符，停止提取')
    os.umask(0o022)
    dest.mkdir(parents=True, mode=0o755)
    archive_path = dest / filename
    archive_path.write_bytes(payload)
    content = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(archive_path)])
    binaries = []
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith('/3proxy'):
                body = archive.extractfile(member).read()
                if body.startswith(b'\x7fELF'):
                    binaries.append(body)
    if len(binaries) != 1 or struct.unpack('<H', binaries[0][18:20])[0] != machine:
        raise RuntimeError('官方包中的程序架构或数量不符')
    binary = dest / '3proxy'
    binary.write_bytes(binaries[0])
    binary.chmod(0o755)
    print(json.dumps({'binary': str(binary), 'architecture': arch, 'package_sha256': expected,
                      'binary_sha256': hashlib.sha256(binaries[0]).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

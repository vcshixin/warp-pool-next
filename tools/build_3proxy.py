"""校验固定版本源码并在新的目录构建，不执行 make install。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

VERSION = '1.0.0'
SHA256 = '35b07de1046f3aaeac4a7085101b7e5c453efa3527cbdc42a84690366c7ecfa8'
URL = 'https://codeload.github.com/3proxy/3proxy/tar.gz/refs/tags/' + VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='尚不存在的构建目录')
    parser.add_argument('--archive', help='离线源码压缩包；省略时使用交付包 vendor 或下载')
    parser.add_argument('--jobs', type=int, default=1, choices=range(1, 9))
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('在目标 Linux 机器上构建')
    dest = Path(args.output).resolve()
    if dest.exists():
        parser.error('构建目录已存在，拒绝覆盖')
    bundled = Path(__file__).resolve().parents[1] / 'vendor/3proxy-1.0.0-source.tar.gz'
    archive = Path(args.archive) if args.archive else bundled
    if archive.exists():
        payload = archive.read_bytes()
    elif args.archive:
        parser.error('指定的离线源码包不存在')
    else:
        request = urllib.request.Request(URL, headers={'User-Agent': 'warp-pool-next-build'})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(4 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != SHA256:
        parser.error('源码 SHA256 不符，停止构建')
    dest.mkdir(parents=True)
    source_file = dest / '3proxy-source.tar.gz'
    source_file.write_bytes(payload)
    with tarfile.open(source_file) as source:
        for member in source.getmembers():
            target = (dest / member.name).resolve()
            if dest not in target.parents or not (member.isfile() or member.isdir()):
                parser.error('源码包存在不允许的路径或成员类型')
        source.extractall(dest)
    root = dest / ('3proxy-' + VERSION)
    # 本服务只需要 SOCKS5 TCP 转发；不引入额外插件或 TLS 终止库。
    command = ['make', '-f', 'Makefile.Linux', '-j' + str(args.jobs), 'PLUGINS=',
               'OPENSSL_CHECK=false', 'WOLFSSL_CHECK=false', 'PCRE_CHECK=false', 'PAM_CHECK=false']
    with (dest / 'build.log').open('wb') as output:
        subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, check=True)
    binary = root / 'bin/3proxy'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('构建结束但没有可执行的 3proxy 文件')
    print(json.dumps({'version': VERSION, 'source_sha256': SHA256, 'binary': str(binary),
                      'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

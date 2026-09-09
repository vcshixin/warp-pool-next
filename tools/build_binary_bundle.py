"""在 CI 中打包自带 Python 运行时的控制器和已编译的代理，不在服务器现场构建。"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warp_pool import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--arch', choices=['amd64', 'arm64'], required=True)
    parser.add_argument('--proxy-binary', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    version = args.version.removeprefix('v')
    assert version == __version__, '发布标签必须与源码版本一致'
    assert {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine()) == args.arch
    output = Path(args.output).resolve()
    assert not output.exists(), '拒绝覆盖已有构建输出'
    output.mkdir(parents=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                    '--name', 'warp-pool-next', '--paths', str(ROOT), '--distpath', '/frozen',
                    '--workpath', '/freeze-work', '--specpath', '/freeze-work',
                    str(ROOT / 'tools/frozen_entry.py')], cwd=ROOT, check=True)
    source = Path('/frozen/warp-pool-next')
    got = subprocess.check_output([str(source / 'warp-pool-next'), '--version'], text=True).strip()
    assert got == version
    (source / 'bin').mkdir()
    shutil.copy2(args.proxy_binary, source / 'bin/3proxy')
    for name in ['README.md', 'THIRD_PARTY.md']:
        shutil.copy2(ROOT / name, source / name)
    for name in ['deploy', 'examples']:
        shutil.copytree(ROOT / name, source / name)
    (source / 'licenses').mkdir()
    shutil.copy2(ROOT / 'vendor/3proxy-LICENSE.txt', source / 'licenses/3proxy-LICENSE.txt')
    name = f'warp-pool-next-{version}-linux-{args.arch}'
    path = output / (name + '.tar.gz')
    with tarfile.open(path, 'w:gz') as archive:
        archive.add(source, arcname=name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / (name + '.sha256')).write_text(digest + '  ' + path.name + '\n', encoding='utf-8')
    print(json.dumps({'archive': str(path), 'sha256': digest, 'architecture': args.arch,
                      'version': version, 'frozen_controller_smoke_test': True}))


if __name__ == '__main__':
    main()

"""首次安装程序和 systemd 模板，不启动线路，不覆盖已有安装。"""

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy-binary', required=True, help='经过校验的 3proxy 1.0.0 可执行文件')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('需要在目标 Linux 上以 root 安装')
    if sys.version_info < (3, 10):
        parser.error('需要 Python 3.10 或更新版本')
    source = Path(__file__).resolve().parents[1]
    binary = Path(args.proxy_binary).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('3proxy 程序不存在或不可执行')
    for tool in ['ip', 'wg', 'systemctl', 'useradd']:
        if not shutil.which(tool):
            parser.error('缺少系统工具：' + tool)
    dest = Path('/opt/warp-pool-next')
    unit = Path('/etc/systemd/system/warp-pool-next@.service')
    if dest.exists() or unit.exists():
        parser.error('已存在安装目录或 systemd 模板，拒绝覆盖；升级请按 README 保留旧版本')
    try:
        user = pwd.getpwnam('warpnext')
        if user.pw_uid == 0 or user.pw_shell not in ['/usr/sbin/nologin', '/sbin/nologin', '/bin/false']:
            parser.error('warpnext 已存在且不是预期的非登录服务账户')
    except KeyError:
        subprocess.run(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin',
                        '--user-group', 'warpnext'], check=True)
    os.umask(0o022)
    dest.mkdir(mode=0o755)
    for name in ['warp_pool', 'tools', 'deploy', 'tests', 'vendor', 'examples']:
        shutil.copytree(source / name, dest / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for file in ['README.md', 'THIRD_PARTY.md']:
        shutil.copy2(source / file, dest / file)
    (dest / 'bin').mkdir(mode=0o755)
    shutil.copy2(binary, dest / 'bin/3proxy')
    (dest / 'bin/3proxy').chmod(0o755)
    for directory in ['/etc/warp-pool-next', '/var/lib/warp-pool-next']:
        Path(directory).mkdir(mode=0o700, exist_ok=True)
    # 使用独占创建，不因复制操作悄悄替换管理员已有的服务模板。
    with unit.open('xb') as output:
        output.write((source / 'deploy/warp-pool-next@.service').read_bytes())
    unit.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print(json.dumps({'installed': str(dest), 'unit_template': str(unit), 'started': False,
                      'note': '程序已安装；先导入配置并检查，再自行启动实例。'}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

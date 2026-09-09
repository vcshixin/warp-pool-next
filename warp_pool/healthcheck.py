"""容器健康检查只读取轻量状态并确认监听，不重复加载整池密钥或探测所有线路。"""

import argparse
import datetime
import json
import os
from pathlib import Path
import socket
import sys


def check(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    directory = Path(config.get('state_dir', '/var/lib/warp-pool-next/' + config['instance']))
    if not directory.is_absolute():
        directory = path.parent / directory
    status = json.loads((directory / 'status.json').read_text())
    if status.get('instance') != config['instance'] or not status.get('running') or not status.get('ready'):
        raise ValueError('服务未达到就绪条件')
    age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(status['utc'])).total_seconds()
    if not 0 <= age <= 120:
        raise ValueError('服务状态已过期')
    for field in ['controller_pid', 'proxy_pid']:
        pid = status[field]
        if type(pid) is not int or pid <= 0:
            raise ValueError('服务进程编号无效')
        os.kill(pid, 0)
    host = config.get('listen', '127.0.0.1')
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    with socket.create_connection((host, config.get('port', 1080)), timeout=2):
        pass
    return {'healthy': True, 'available_lines': status['available_lines']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/warp-pool-next/config.json')
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.config)))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'healthy': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

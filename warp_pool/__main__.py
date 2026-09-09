"""管理命令不输出代理密码或 WireGuard 私钥。"""

import argparse
import json
import os
import sys

from .config import load_config
from . import __version__


def main():
    parser = argparse.ArgumentParser(description='轻量 WARP IPv6 代理池')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['check', 'run', 'status'])
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.command == 'check':
            print(json.dumps({'valid': True, 'instance': cfg.instance, 'mode': cfg.mode,
                              'lines': len(cfg.lines), 'unique_keys': len(cfg.lines),
                              'note': '配置校验不能证明公网出口已可用或互不重复。'}, ensure_ascii=False))
        elif args.command == 'status':
            value = json.loads((cfg.state_dir / 'status.json').read_text())
            # 状态文件只能用于展示，不能把旧的 running 字段当作进程存活证明。
            value['process_check'] = 'not_checked'
            if os.name == 'posix':
                try:
                    os.kill(value['controller_pid'], 0)
                    value['process_check'] = 'pid_exists_requires_service_verification'
                except ProcessLookupError:
                    value['process_check'] = 'not_running'
                    value['running'] = value['ready'] = False
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            from .service import Service
            Service(cfg).run()
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

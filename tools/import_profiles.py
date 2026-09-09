"""离线导入已有 WARP 配置；只创建新目录，不注册账号或覆盖已有配置。"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warp_pool.config import check, credential, load_config, load_profile, private_text
from warp_pool.storage import atomic_write


def create_pool(profiles_dir, output, instance, mode='users', listen='127.0.0.1', port=1080,
                port_base=20000, minimum=None, shared_password_file=None, pattern='*.conf'):
    check(re.fullmatch(r'[a-z][a-z0-9-]{0,23}', instance), '实例名格式无效')
    check(mode in ['users', 'ports', 'both'], '入口模式无效')
    listen = str(ipaddress.ip_address(listen))
    source = Path(profiles_dir).resolve()
    check(source.is_dir(), '输入配置目录不存在')
    files = sorted(source.rglob(pattern))
    check(1 <= len(files) <= 2048, '应提供 1 至 2048 份配置；可用 --pattern 指定匹配规则')
    profiles = [load_profile(path) for path in files]
    check(len({row.source for row in profiles}) == len(profiles), '输入配置的隧道 IPv6 有重复')
    check(len({row.fingerprint for row in profiles}) == len(profiles), '输入配置的私钥有重复')
    check(1 <= port <= 65535 and 1 <= port_base and port_base + len(profiles) <= 65535, '端口范围无效')
    check(mode != 'both' or port not in range(port_base + 1, port_base + len(profiles) + 1), '两种入口端口冲突')
    minimum = len(profiles) if minimum is None else minimum
    check(1 <= minimum <= len(profiles), '要求的独立出口数必须在 1 和配置总数之间')
    shared = None
    if shared_password_file:
        check(mode == 'ports', '只有 ports 模式可共用同一用户名和密码')
        shared = credential(private_text(shared_password_file).strip(), '密码')
        check(len(shared) >= 16, '密码至少需要 16 位')
    dest = Path(output).resolve()
    check(not dest.exists(), '输出目录已存在，拒绝覆盖；请使用新的空目录路径')
    os.umask(0o077)
    dest.mkdir(parents=True, mode=0o700)
    (dest / 'profiles').mkdir(mode=0o700)
    (dest / 'passwords').mkdir(mode=0o700)
    rows, clients, mapping = [], ['line_id\tmode\thost\tport\tusername\tpassword'], []
    for index, (origin, profile) in enumerate(zip(files, profiles), 1):
        name = f'{index:04d}'
        username = 'warppool' if shared else 'warp-' + name
        password = shared or secrets.token_urlsafe(24)
        atomic_write(dest / 'passwords' / name, password + '\n')
        canonical = ('[Interface]\nPrivateKey = ' + profile.private_key + '\nAddress = ' + profile.source +
                     '/128\n[Peer]\nPublicKey = ' + profile.public_key + '\nEndpoint = ' + profile.endpoint +
                     '\nAllowedIPs = ::/0\n')
        atomic_write(dest / 'profiles' / (name + '.conf'), canonical)
        row = {'id': index, 'username': username, 'password_file': 'passwords/' + name,
               'profile': 'profiles/' + name + '.conf', 'enabled': True}
        if mode != 'users':
            row['port'] = port_base + index
        rows.append(row)
        if mode in ['users', 'both']:
            clients.append(f'{index}\tusers\t{listen}\t{port}\t{username}\t{password}')
        if mode in ['ports', 'both']:
            clients.append(f'{index}\tports\t{listen}\t{port_base + index}\t{username}\t{password}')
        mapping.append({'id': index, 'input_file': str(origin), 'source_ipv6': profile.source,
                        'key_fingerprint': profile.fingerprint})
    config = {'instance': instance, 'state_dir': '/var/lib/warp-pool-next/' + instance,
              'listen': listen, 'mode': mode, 'port': port, 'proxy_user': 'warpnext', 'exit_policy': 'follow_line',
              'health_interval': 300, 'repair_interval': 30, 'min_unique_exits': minimum, 'lines': rows}
    atomic_write(dest / 'config.json', config)
    atomic_write(dest / 'clients.tsv', '\n'.join(clients) + '\n')
    atomic_write(dest / 'import-map.json', mapping)
    load_config(dest / 'config.json')
    return {'created': str(dest), 'candidate_lines': len(rows), 'required_unique_exits': minimum,
            'mode': mode, 'note': '尚未启动；账号数不是实际独立公网 IPv6 数。clients.tsv 含密码，应保持私有。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--mode', choices=['users', 'ports', 'both'], default='users')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=1080)
    parser.add_argument('--port-base', type=int, default=20000)
    parser.add_argument('--min-unique-exits', type=int)
    parser.add_argument('--shared-password-file')
    parser.add_argument('--pattern', default='*.conf')
    args = parser.parse_args()
    try:
        result = create_pool(args.profiles_dir, args.output, args.instance, args.mode, args.listen, args.port,
                             args.port_base, args.min_unique_exits, args.shared_password_file, args.pattern)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

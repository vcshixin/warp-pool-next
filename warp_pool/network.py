"""为每条线路建立源地址路由和拒绝路由，绝不接管宿主默认路由。"""

import ipaddress
import json
from pathlib import Path
import socket
import subprocess

from .storage import atomic_write


def resolve_endpoint(endpoint):
    host, port = endpoint.rsplit(':', 1)
    host = host.strip('[]')
    choices = socket.getaddrinfo(host, int(port), socket.AF_UNSPEC, socket.SOCK_DGRAM)
    # 隧道外层优先使用已在本次机器上验证的 IPv4 UDP；目标网站依然只走 IPv6。
    choices.sort(key=lambda row: row[0] != socket.AF_INET)
    target = choices[0][4][0]
    target = '[' + target + ']' if ':' in target else target
    return target + ':' + port


class Network:
    def __init__(self, cfg, execute=None):
        self.cfg = cfg
        self.execute = execute or subprocess.run
        self.manifest_path = cfg.state_dir / 'network.json'
        self.transport_path = cfg.state_dir / 'transport.json'
        self.transport = json.loads(self.transport_path.read_text()) if self.transport_path.exists() else {}
        self.records = [self.record(line) for line in cfg.lines]
        self.started = False
        self.proxy_uid = None
        self.blocked = set()

    def record(self, line):
        return {'id': line.id, 'interface': self.cfg.interface(line), 'source': line.profile.source,
                'table': self.cfg.table_base + line.id, 'priority': self.cfg.rule_base + line.id,
                'block_priority': self.cfg.rule_base - 4096 + line.id,
                'alias': f'warp-pool-next:{self.cfg.instance}:{line.id}:{line.profile.fingerprint[:16]}'}

    def run(self, args, input=None, required=True):
        result = self.execute(args, input=input, capture_output=True, text=True, timeout=20)
        if required and result.returncode:
            # WireGuard 的错误输出可能含配置片段，不向日志拼接原始标准错误。
            raise RuntimeError('网络命令失败：' + ' '.join(str(arg) for arg in args[:4]))
        return result

    def snapshot(self):
        def query(args):
            return json.loads(self.run(['ip', '-j'] + args).stdout)
        peers = {}
        for row in self.run([str(self.cfg.wg_binary), 'show', 'all', 'peers']).stdout.splitlines():
            parts = row.split()
            if parts:
                peers[parts[0]] = parts[1:]
        return {'links': {row['ifname']: row for row in query(['link', 'show'])}, 'peers': peers,
                'addresses': {row['ifname']: row.get('addr_info', []) for row in query(['-6', 'address', 'show'])},
                'rules': query(['-6', 'rule', 'show']), 'routes': query(['-6', 'route', 'show', 'table', 'all'])}

    @staticmethod
    def matching_rule(row, item):
        try:
            return (row.get('priority') == item['priority'] and int(row.get('table', -1)) == item['table']
                    and ipaddress.ip_network(row.get('src', '::/0')) == ipaddress.ip_network(item['source'] + '/128'))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def table_routes(snap, item):
        return [row for row in snap['routes'] if str(row.get('table')) == str(item['table'])]

    def matching_block(self, row, item):
        try:
            return (row.get('priority') == item['block_priority'] and row.get('action') == 'prohibit'
                    and row.get('uid_start') == self.proxy_uid and row.get('uid_end') == self.proxy_uid
                    and ipaddress.ip_network(row.get('src', '::/0')) == ipaddress.ip_network(item['source'] + '/128'))
        except (ValueError, TypeError):
            return False

    def block_args(self, item):
        return ['priority', str(item['block_priority']), 'from', item['source'] + '/128',
                'uidrange', f'{self.proxy_uid}-{self.proxy_uid}', 'prohibit']

    def guard(self, item, rules):
        for row in rules:
            if row.get('priority') == item['block_priority'] and not self.matching_block(row, item):
                raise RuntimeError('出口隔离规则优先级已被其他配置使用')
        if not any(self.matching_block(row, item) for row in rules):
            self.run(['ip', '-6', 'rule', 'add'] + self.block_args(item))
            rules.append({'priority': item['block_priority'], 'src': item['source'], 'action': 'prohibit',
                          'uid_start': self.proxy_uid, 'uid_end': self.proxy_uid})
        self.blocked.add(item['id'])

    def set_available(self, available, unblock=False):
        wanted = {item['id'] for item in self.records} - set(available)
        add = wanted - self.blocked
        remove = self.blocked - wanted if unblock else set()
        if not add and not remove:
            return
        rules = json.loads(self.run(['ip', '-j', '-6', 'rule', 'show']).stdout)
        for item in self.records:
            if item['id'] in add:
                self.guard(item, rules)
            elif item['id'] in remove:
                matches = [row for row in rules if row.get('priority') == item['block_priority']]
                if any(not self.matching_block(row, item) for row in matches):
                    raise RuntimeError('出口隔离规则被其他配置替换')
                for _ in matches:
                    self.run(['ip', '-6', 'rule', 'del'] + self.block_args(item))
                self.blocked.discard(item['id'])

    def assert_owned(self, snap, item, allow_missing=True):
        link = snap['links'].get(item['interface'])
        if link and link.get('ifalias') != item['alias']:
            raise RuntimeError('同名接口不属于本服务：' + item['interface'])
        if not link and not allow_missing:
            raise RuntimeError('本服务接口不存在：' + item['interface'])
        for row in snap['rules']:
            if row.get('priority') == item['priority'] and not self.matching_rule(row, item):
                raise RuntimeError('路由规则优先级已被其他配置使用')
            if row.get('priority') == item['block_priority'] and not self.matching_block(row, item):
                raise RuntimeError('出口隔离规则优先级已被其他配置使用')
        for row in self.table_routes(snap, item):
            guard = row.get('type') == 'unreachable' and row.get('metric') == 32760
            path = row.get('dev') == item['interface'] and row.get('metric') == 10
            if row.get('dst') != 'default' or not (guard or path):
                raise RuntimeError('独立路由表内存在其他配置')

    def prepare(self):
        import pwd
        self.proxy_uid = pwd.getpwnam(self.cfg.proxy_user).pw_uid
        if self.proxy_uid == 0:
            raise RuntimeError('代理必须使用非 root 账户，才能与健康探测分开实施出口隔离')
        snap = self.snapshot()
        existing = self.manifest_path.exists()
        previous = json.loads(self.manifest_path.read_text()) if existing else None
        if previous and previous.get('nodes'):
            if previous != {'instance': self.cfg.instance, 'proxy_uid': self.proxy_uid, 'nodes': self.records}:
                raise RuntimeError('存留网络状态与当前配置不一致；先用原配置正常停止服务')
        else:
            # 新实例不接管任何已有接口、规则或路由表，即使名字碰巧相同。
            for item in self.records:
                if item['interface'] in snap['links'] or self.table_routes(snap, item) or any(
                        row.get('priority') in [item['priority'], item['block_priority']] for row in snap['rules']):
                    raise RuntimeError('新实例的接口、路由表或优先级已被占用')
        sources = {row['local']: name for name, rows in snap['addresses'].items() for row in rows if 'local' in row}
        for item in self.records:
            if item['source'] in sources and sources[item['source']] != item['interface']:
                raise RuntimeError('源 IPv6 已用于其他接口')
            self.assert_owned(snap, item)
        atomic_write(self.manifest_path, {'instance': self.cfg.instance, 'proxy_uid': self.proxy_uid, 'nodes': self.records})
        self.started = True
        for line, item in zip(self.cfg.lines, self.records):
            self.guard(item, snap['rules'])
            self.ensure(line, item, snap)

    def ensure(self, line, item, snap):
        self.assert_owned(snap, item)
        interface = item['interface']
        created = interface not in snap['links']
        if created:
            self.run(['ip', 'link', 'add', interface, 'type', 'wireguard'])
            self.run(['ip', 'link', 'set', 'dev', interface, 'alias', item['alias']])
        changed_peer = created or line.profile.public_key not in snap.get('peers', {}).get(interface, [])
        if changed_peer:
            # 普通网卡或路由恢复不重设 Peer，避免无意重建隧道会话。
            cached = self.transport.get(str(line.id), {})
            same = (cached.get('fingerprint') == line.profile.fingerprint and
                    cached.get('configured_endpoint') == line.profile.endpoint)
            endpoint = cached['endpoint'] if same else resolve_endpoint(line.profile.endpoint)
            host, port = endpoint.rsplit(':', 1)
            ipaddress.ip_address(host.strip('[]'))
            if not 1 <= int(port) <= 65535:
                raise RuntimeError('缓存的隧道端点端口无效')
            listen = cached.get('listen_port', 0) if same else 0
            if type(listen) is not int or not 0 <= listen <= 65535:
                raise RuntimeError('缓存的隧道监听端口无效')
            text = ('[Interface]\nPrivateKey = ' + line.profile.private_key + f'\nListenPort = {listen}\n' +
                    '[Peer]\nPublicKey = ' + line.profile.public_key + '\nAllowedIPs = ::/0\nEndpoint = ' +
                    endpoint + '\nPersistentKeepalive = 15\n')
            self.run([str(self.cfg.wg_binary), 'setconf', interface, '/dev/stdin'], input=text)
        self.run(['ip', '-6', 'addr', 'replace', item['source'] + '/128', 'dev', interface, 'nodad'])
        self.run(['ip', 'link', 'set', 'dev', interface, 'mtu', '1280', 'up'])
        if changed_peer:
            actual_port = int(self.run([str(self.cfg.wg_binary), 'show', interface, 'listen-port']).stdout.strip())
            self.transport[str(line.id)] = {'fingerprint': line.profile.fingerprint,
                'configured_endpoint': line.profile.endpoint, 'endpoint': endpoint, 'listen_port': actual_port}
            atomic_write(self.transport_path, self.transport)
        self.run(['ip', '-6', 'route', 'replace', 'unreachable', 'default', 'metric', '32760', 'table', str(item['table'])])
        self.run(['ip', '-6', 'route', 'replace', 'default', 'dev', interface, 'metric', '10', 'table', str(item['table'])])
        if not any(self.matching_rule(row, item) for row in snap['rules']):
            self.run(['ip', '-6', 'rule', 'add', 'priority', str(item['priority']), 'from', item['source'] + '/128',
                      'lookup', str(item['table'])])

    def repair(self):
        snap = self.snapshot()
        repaired = []
        for line, item in zip(self.cfg.lines, self.records):
            self.assert_owned(snap, item)
            if item['id'] in self.blocked:
                self.guard(item, snap['rules'])
            link = snap['links'].get(item['interface'], {})
            addresses = snap['addresses'].get(item['interface'], [])
            routes = self.table_routes(snap, item)
            intact = ('UP' in link.get('flags', []) and
                      line.profile.public_key in snap.get('peers', {}).get(item['interface'], []) and
                      any(row.get('local') == item['source'] for row in addresses) and
                      any(self.matching_rule(row, item) for row in snap['rules']) and
                      any(row.get('type') == 'unreachable' and row.get('metric') == 32760 for row in routes) and
                      any(row.get('dev') == item['interface'] and row.get('metric') == 10 for row in routes))
            if not intact:
                self.guard(item, snap['rules'])
                self.ensure(line, item, snap)
                repaired.append(line.id)
        return repaired

    def close(self):
        if not self.started:
            return
        snap = self.snapshot()
        for item in reversed(self.records):
            self.assert_owned(snap, item)
            interface = item['interface']
            # 先撤销隧道，最后移除拒绝路由和规则；此时代理子进程已经退出。
            if interface in snap['links']:
                self.run(['ip', 'link', 'delete', interface])
            for row in self.table_routes(snap, item):
                if row.get('type') == 'unreachable':
                    self.run(['ip', '-6', 'route', 'del', 'unreachable', 'default', 'metric', '32760',
                              'table', str(item['table'])])
            if any(self.matching_rule(row, item) for row in snap['rules']):
                self.run(['ip', '-6', 'rule', 'del', 'priority', str(item['priority']),
                          'from', item['source'] + '/128', 'lookup', str(item['table'])])
            for _ in [row for row in snap['rules'] if self.matching_block(row, item)]:
                self.run(['ip', '-6', 'rule', 'del'] + self.block_args(item))
        atomic_write(self.manifest_path, {'instance': self.cfg.instance, 'proxy_uid': self.proxy_uid, 'nodes': []})
        self.started = False

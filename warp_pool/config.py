"""校验线路配置并生成固定出口规则，禁止配置内容变成代理指令。"""

import base64
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re


class ConfigError(ValueError):
    pass


def check(condition, message):
    if not condition:
        raise ConfigError(message)


def private_text(path):
    path = Path(path)
    check(path.is_file() and not path.is_symlink(), f'需要普通配置文件：{path.name}')
    if os.name == 'posix':
        check(path.stat().st_mode & 0o077 == 0, f'私有文件权限应为 600：{path.name}')
    return path.read_text(encoding='utf-8')


def key32(value):
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Profile:
    source: str
    private_key: str = field(repr=False)
    public_key: str
    endpoint: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.private_key.encode()).hexdigest()


def load_profile(path):
    fields = {}
    section = None
    for raw in private_text(path).splitlines():
        raw = raw.strip()
        if not raw or raw.startswith(('#', ';')):
            continue
        if raw.startswith('[') and raw.endswith(']'):
            section = raw[1:-1]
            check(section in ['Interface', 'Peer'], '不支持的 WireGuard 配置节')
            if section == 'Peer':
                check('_peer_seen' not in fields, '每条线路必须只有一个 Peer')
                fields['_peer_seen'] = True
            continue
        check('=' in raw and section is not None, 'WireGuard 配置格式错误')
        name, value = [part.strip() for part in raw.split('=', 1)]
        key = section + '.' + name
        if key == 'Interface.Address':
            fields.setdefault(key, []).extend(part.strip() for part in value.split(','))
        else:
            check(key not in fields, f'WireGuard 配置字段重复：{name}')
            fields[key] = value
    try:
        addresses = [ipaddress.ip_interface(value) for value in fields['Interface.Address']]
        v6 = [value for value in addresses if value.version == 6]
        check(len(v6) == 1 and v6[0].network.prefixlen == 128, '每条线路需要一个 IPv6 /128 地址')
        check(v6[0].ip.is_global and not v6[0].ip.ipv4_mapped, 'WARP 线路地址必须是全局 IPv6')
        private = fields['Interface.PrivateKey']
        public = fields['Peer.PublicKey']
        check(key32(private) and key32(public), 'WireGuard 密钥格式无效')
        endpoint = fields['Peer.Endpoint']
        host, port = endpoint.rsplit(':', 1)
        host = host.strip('[]')
        check(re.fullmatch(r'[A-Za-z0-9.:_-]+', host) is not None and 1 <= int(port) <= 65535,
              'WireGuard Endpoint 格式无效')
    except (KeyError, ValueError) as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError('WireGuard 配置缺少必要字段或地址格式无效') from None
    return Profile(str(v6[0].ip), private, public, endpoint)


def credential(value, kind):
    check(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.@+-]{1,128}', value),
          f'{kind}只支持 1 至 128 位字母、数字或 _.@+-，防止配置注入')
    return value


@dataclass(frozen=True)
class Line:
    id: int
    username: str
    password: str = field(repr=False)
    profile: Profile = field(repr=False)
    port: int | None = None
    enabled: bool = True
    expected_exit: str | None = None


@dataclass(frozen=True)
class RandomUser:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    path: Path
    instance: str
    state_dir: Path
    proxy_binary: Path
    wg_binary: Path
    listen: str
    mode: str
    port: int
    rule_base: int
    table_base: int
    health_interval: int
    repair_interval: int
    probe_timeout: int
    probe_workers: int
    failures_before_down: int
    min_unique_exits: int
    max_connections: int
    per_port_connections: int
    proxy_user: str
    dns_server: str
    exit_policy: str
    lines: tuple[Line, ...]
    random_users: tuple[RandomUser, ...] = ()

    @property
    def topology(self):
        return (self.instance, str(self.state_dir), str(self.proxy_binary), str(self.wg_binary),
                self.listen, self.mode, self.port, self.rule_base, self.table_base, self.proxy_user,
                tuple((n.id, n.username, n.profile.source, n.profile.fingerprint, n.profile.public_key,
                       n.profile.endpoint, n.port)
                      for n in self.lines))

    def interface(self, line):
        prefix = hashlib.sha256(self.instance.encode()).hexdigest()[:6]
        return f'wp{prefix}{line.id:04d}'


def load_config(path):
    path = Path(path).resolve()
    try:
        raw = json.loads(private_text(path))
    except json.JSONDecodeError:
        raise ConfigError('主配置不是有效 JSON') from None
    check(isinstance(raw, dict), '配置必须是 JSON 对象')
    allowed = {f.name for f in Config.__dataclass_fields__.values()} - {'path'}
    check(not set(raw) - allowed, '主配置存在未知字段')
    check(isinstance(raw.get('instance'), str) and
          re.fullmatch(r'[a-z][a-z0-9-]{0,23}', raw['instance']), 'instance 格式错误')
    base = path.parent

    def resolve(value):
        check(isinstance(value, str) and value and '\n' not in value, '文件路径无效')
        p = Path(value)
        return (base / p).resolve() if not p.is_absolute() else p.resolve()

    state = resolve(raw.get('state_dir', '/var/lib/warp-pool-next/' + raw['instance']))
    check(state != state.parent and state != base, '状态目录必须是独立子目录')
    try:
        bind = str(ipaddress.ip_address(raw.get('listen', '127.0.0.1')))
        dns = str(ipaddress.ip_address(raw.get('dns_server', '1.1.1.1')))
    except ValueError:
        raise ConfigError('监听地址和 DNS 服务器必须使用 IP 地址') from None
    mode = raw.get('mode', 'users')
    check(mode in ['users', 'ports', 'both'], 'mode 应为 users、ports 或 both')
    exit_policy = raw.get('exit_policy', 'follow_line')
    check(exit_policy in ['follow_line', 'fixed_ip'], 'exit_policy 应为 follow_line 或 fixed_ip')
    defaults = {'port': 1080, 'table_base': 61000, 'rule_base': 20000,
                'health_interval': 300, 'repair_interval': 30, 'probe_timeout': 8,
                'probe_workers': 4, 'failures_before_down': 2, 'min_unique_exits': 1,
                'max_connections': 1024, 'per_port_connections': 64}
    limits = {'port': (1, 65535), 'table_base': (1000, 1000000000), 'rule_base': (6000, 30000),
              'health_interval': (10, 86400), 'repair_interval': (5, 3600), 'probe_timeout': (1, 30),
              'probe_workers': (1, 16), 'failures_before_down': (1, 10), 'min_unique_exits': (1, 2048),
              'max_connections': (1, 8192), 'per_port_connections': (1, 1024)}
    values = {}
    for name, default in defaults.items():
        value = raw.get(name, default)
        check(type(value) is int and limits[name][0] <= value <= limits[name][1], f'{name} 超出允许范围')
        values[name] = value
    source_rows = raw.get('lines')
    check(isinstance(source_rows, list) and 1 <= len(source_rows) <= 2048, '需要 1 至 2048 条线路')
    lines = []
    for row in source_rows:
        check(isinstance(row, dict) and not set(row) - {'id', 'username', 'password_file', 'profile', 'port', 'enabled', 'expected_exit'},
              '线路存在未知字段')
        index = row.get('id')
        check(type(index) is int and 1 <= index <= 2048, '线路 id 应为 1 至 2048')
        enabled = row.get('enabled', True)
        check(type(enabled) is bool, 'enabled 必须为布尔值')
        port = row.get('port')
        check(port is None or type(port) is int and 1 <= port <= 65535, '线路端口无效')
        check(mode == 'users' or port is not None, '多端口模式需要每条线路配置 port')
        username = credential(row.get('username'), '用户名')
        password = credential(private_text(resolve(row.get('password_file'))).strip(), '密码')
        check(len(password) >= 16, '密码至少需要 16 位')
        expected = row.get('expected_exit')
        if expected is not None:
            try:
                addr = ipaddress.IPv6Address(expected)
                check(addr.is_global and not addr.ipv4_mapped, 'expected_exit 必须是全局 IPv6')
                expected = str(addr)
            except ipaddress.AddressValueError:
                raise ConfigError('expected_exit 格式无效') from None
        lines.append(Line(index, username, password, load_profile(resolve(row.get('profile'))), port, enabled, expected))
    lines.sort(key=lambda n: n.id)
    identity_fields = [('id', '线路 id')] + ([] if mode == 'ports' else [('username', '用户名')])
    for attr, label in identity_fields:
        check(len({getattr(n, attr) for n in lines}) == len(lines), label + '不能重复')
    passwords = {}
    for line in lines:
        check(line.username not in passwords or passwords[line.username] == line.password,
              '多个端口共用用户名时，密码必须一致')
        passwords[line.username] = line.password
    check(len({n.profile.source for n in lines}) == len(lines), '隧道 IPv6 地址不能重复')
    check(len({n.profile.fingerprint for n in lines}) == len(lines), '不能重复使用同一 WireGuard 私钥')
    if mode != 'users':
        check(len({n.port for n in lines}) == len(lines), '监听端口不能重复')
        check(mode != 'both' or values['port'] not in {n.port for n in lines}, '共享端口和独立端口冲突')
    check(values['min_unique_exits'] <= len(lines), '最少独立出口数不能超过线路数')
    check(values['rule_base'] + max(n.id for n in lines) < 32766, '路由规则优先级必须在 main 表之前')
    proxy_user = raw.get('proxy_user', 'warpnext')
    check(isinstance(proxy_user, str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', proxy_user), '代理用户名称无效')
    random_rows = raw.get('random_users', [])
    check(isinstance(random_rows, list) and len(random_rows) <= 32, '随机聚合账号最多 32 个')
    check(not random_rows or mode == 'users', '随机聚合只允许使用单端口 users 模式')
    check(not random_rows or len(lines) <= 1000, '随机聚合最多支持 1000 条候选线路')
    random_users = []
    for row in random_rows:
        check(isinstance(row, dict) and set(row) == {'username', 'password_file'}, '随机聚合账号字段无效')
        username = credential(row['username'], '随机聚合用户名')
        check(username not in passwords, '随机聚合用户名不能与线路或其他聚合账号重复')
        password = credential(private_text(resolve(row['password_file'])).strip(), '随机聚合密码')
        check(len(password) >= 16, '密码至少需要 16 位')
        passwords[username] = password
        random_users.append(RandomUser(username, password))
    return Config(path, raw['instance'], state, resolve(raw.get('proxy_binary', '/opt/warp-pool-next/bin/3proxy')),
                  resolve(raw.get('wg_binary', '/usr/bin/wg')), bind, mode,
                  **values, proxy_user=proxy_user, dns_server=dns, exit_policy=exit_policy, lines=tuple(lines),
                  random_users=tuple(random_users))


def proxy_config(cfg, available):
    dns = '[' + cfg.dns_server + ']' if ':' in cfg.dns_server else cfg.dns_server
    passwords = {line.username: line.password for line in cfg.lines}
    passwords.update({user.username: user.password for user in cfg.random_users})
    result = ['nserver ' + dns, 'nscache6 4096', 'timeouts 1 5 8 30 180 3600 5 10 10 5 5',
              'noforce']
    result += ['users ' + name + ':CL:' + password for name, password in passwords.items()]
    result += ['auth strong', f'connlim {cfg.max_connections} 0 *']
    deny = 'deny * * 0.0.0.0/0,::ffff:0:0/96,::/128,::1/128,fc00::/7,fe80::/10,ff00::/8'

    def rules(nodes, aggregate=False):
        result.extend(['flush', deny])
        active = []
        for node in nodes:
            if node.enabled and node.id in available:
                active.append(node)
                result.extend([f'allow {node.username} * * * CONNECT',
                               f'parent 1000 extip {node.profile.source} 0'])
        if aggregate and active:
            # 3proxy 每个父链的权重合计 1000；在已验证线路间按连接随机选择。
            weight, remainder = divmod(1000, len(active))
            for user in cfg.random_users:
                result.append(f'allow {user.username} * * * CONNECT')
                result.extend(f'parent {weight + (index < remainder)} extip {node.profile.source} 0'
                              for index, node in enumerate(active))
        result.append('deny *')

    if cfg.mode in ['users', 'both']:
        result.append('maxconn ' + str(cfg.max_connections))
        rules(cfg.lines, aggregate=True)
        result.append(f'socks -6 -i{cfg.listen} -p{cfg.port}')
    if cfg.mode in ['ports', 'both']:
        result.append('maxconn ' + str(cfg.per_port_connections))
        for node in cfg.lines:
            rules([node])
            result.append(f'socks -6 -i{cfg.listen} -p{node.port}')
    return '\n'.join(result) + '\n'

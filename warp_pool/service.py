"""控制面保持低频工作；代理转发交给一个成熟的 3proxy 进程。"""

import concurrent.futures
import datetime
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import threading
import time

from .config import load_config, proxy_config
from .network import Network
from .storage import atomic_write, owned_directory


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(event, **fields):
    print(json.dumps({'utc': utc(), 'event': event, **fields}, ensure_ascii=False), flush=True)


TLS = ssl.create_default_context()
PROBE_HOST = 'cloudflare.com'


def probe(line, timeout):
    try:
        addresses = socket.getaddrinfo(PROBE_HOST, 443, socket.AF_INET6, socket.SOCK_STREAM)
        unique = list(dict.fromkeys(row[4][0] for row in addresses))[:2]
        for target in unique:
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as raw:
                    raw.settimeout(timeout)
                    raw.bind((line.profile.source, 0, 0, 0))
                    raw.connect((target, 443, 0, 0))
                    with TLS.wrap_socket(raw, server_hostname=PROBE_HOST) as conn:
                        conn.sendall(('GET /cdn-cgi/trace HTTP/1.1\r\nHost: ' + PROBE_HOST +
                                      '\r\nConnection: close\r\nUser-Agent: warp-pool-next-health\r\n\r\n').encode())
                        response = http.client.HTTPResponse(conn)
                        response.begin()
                        payload = response.read(16384).decode('utf-8', errors='replace')
                        fields = dict(row.split('=', 1) for row in payload.splitlines() if '=' in row)
                        address = ipaddress.IPv6Address(fields.get('ip', '::'))
                        if response.status == 200 and fields.get('warp') == 'on' and address.is_global and not address.ipv4_mapped:
                            return {'ok': True, 'exit_ipv6': str(address)}
            except (OSError, ValueError, http.client.HTTPException):
                continue
        return {'ok': False, 'reason': 'ipv6_warp_probe_failed'}
    except OSError:
        return {'ok': False, 'reason': 'probe_dns_failed'}


class Proxy:
    def __init__(self, cfg):
        import pwd
        self.cfg = cfg
        self.user = pwd.getpwnam(cfg.proxy_user)
        self.directory = Path('/run') / ('warp-pool-next-' + cfg.instance)
        self.directory.mkdir(mode=0o750, exist_ok=True)
        marker = self.directory / 'owner.json'
        owner = {'owner': 'warp-pool-next:' + cfg.instance, 'state_dir': str(cfg.state_dir)}
        if marker.exists():
            if json.loads(marker.read_text()) != owner:
                raise RuntimeError('代理运行目录归属不一致')
        else:
            if any(self.directory.iterdir()):
                raise RuntimeError('代理运行目录非空且缺少归属标记')
            atomic_write(marker, owner)
        os.chown(self.directory, 0, self.user.pw_gid)
        self.directory.chmod(0o750)
        self.path = self.directory / '3proxy.cfg'
        self.child = None
        self.digest = None
        self.logfile = None

    def publish(self, content):
        digest = hashlib.sha256(content.encode()).hexdigest()
        if digest == self.digest and self.child and self.child.poll() is None:
            return False
        atomic_write(self.path, content, 0o640, gid=self.user.pw_gid)
        if not self.child or self.child.poll() is not None:
            if self.logfile:
                self.logfile.close()
            self.logfile = (self.cfg.state_dir / 'proxy.log').open('ab')
            self.child = subprocess.Popen([str(self.cfg.proxy_binary), str(self.path)],
                                          stdin=subprocess.DEVNULL, stdout=self.logfile, stderr=self.logfile,
                                          user=self.user.pw_uid, group=self.user.pw_gid, extra_groups=[],
                                          start_new_session=True,
                                          env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C'})
            log('proxy_started', pid=self.child.pid)
        else:
            self.child.send_signal(signal.SIGUSR1)
            log('proxy_configuration_reloaded')
        self.digest = digest
        return True

    def close(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        if self.logfile:
            self.logfile.close()


class Health:
    def __init__(self, cfg, pins=None):
        self.cfg = cfg
        self.pins = pins or {}
        for key, value in self.pins.items():
            if not str(key).isdigit() or not ipaddress.IPv6Address(value).is_global:
                raise RuntimeError('出口固定记录格式无效')
        self.rows = {n.id: {'healthy': False, 'reason': 'not_checked', 'failures': 0,
                           'exit_ipv6': None, 'last_checked': None} for n in cfg.lines}

    def update(self, line, result):
        row = self.rows[line.id]
        row['last_checked'] = utc()
        if result['ok']:
            address = result['exit_ipv6']
            expected = line.expected_exit or (self.pins.get(str(line.id)) if self.cfg.exit_policy == 'fixed_ip' else None)
            row['exit_ipv6'] = address
            row['failures'] = 0
            row['healthy'] = expected is None or address == expected
            row['reason'] = 'healthy' if row['healthy'] else 'exit_changed'
            if row['healthy']:
                self.pins[str(line.id)] = address
        else:
            row['failures'] += 1
            row['reason'] = result['reason']
            if row['failures'] >= self.cfg.failures_before_down:
                row['healthy'] = False

    def available(self):
        accepted, seen, duplicates = set(), {}, {}
        for line in self.cfg.lines:
            row = self.rows[line.id]
            if not line.enabled or not row['healthy'] or not row['exit_ipv6']:
                continue
            address = row['exit_ipv6']
            if address in seen:
                duplicates[line.id] = seen[address]
                continue
            seen[address] = line.id
            accepted.add(line.id)
        return accepted, duplicates


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.reload_requested = threading.Event()
        self.network = Network(cfg)
        self.proxy = None
        self.health = None
        self.lockfile = None
        self.started = time.monotonic()

    def snapshot(self, running=True):
        available, duplicates = self.health.available()
        proxy_alive = bool(self.proxy and self.proxy.child and self.proxy.child.poll() is None)
        return {'schema_version': 1, 'utc': utc(), 'instance': self.cfg.instance, 'running': running,
                'controller_pid': os.getpid(), 'proxy_pid': self.proxy.child.pid if self.proxy and self.proxy.child else None,
                'mode': self.cfg.mode, 'exit_policy': self.cfg.exit_policy, 'configured_lines': len(self.cfg.lines),
                'random_usernames': [user.username for user in self.cfg.random_users],
                'checked_lines': sum(row['last_checked'] is not None for row in self.health.rows.values()),
                'available_lines': len(available), 'unique_exits': len(available),
                'duplicate_lines': len(duplicates),
                'ready': running and proxy_alive and len(available) >= self.cfg.min_unique_exits,
                'required_unique_exits': self.cfg.min_unique_exits,
                'lines': [{'id': line.id, 'username': line.username, 'interface': self.cfg.interface(line),
                           'port': self.cfg.port if self.cfg.mode == 'users' else line.port,
                           'enabled': line.enabled, 'available': line.id in available,
                           'duplicate_of': duplicates.get(line.id), **self.health.rows[line.id],
                           'reason': ('disabled' if not line.enabled else 'duplicate_exit' if line.id in duplicates
                                      else self.health.rows[line.id]['reason'])} for line in self.cfg.lines]}

    def publish(self):
        available, _ = self.health.available()
        self.network.set_available(available)
        self.proxy.publish(proxy_config(self.cfg, available))
        self.network.set_available(available, unblock=True)
        atomic_write(self.cfg.state_dir / 'pins.json', self.health.pins)
        atomic_write(self.cfg.state_dir / 'status.json', self.snapshot())

    def reload(self):
        try:
            new = load_config(self.cfg.path)
            if new.topology != self.cfg.topology:
                raise RuntimeError('热更新不允许改变线路身份、端口或网络拓扑；应安排重启')
            if new.probe_workers != self.cfg.probe_workers:
                raise RuntimeError('调整探测工作线程数量需要重启')
        except (ValueError, OSError, RuntimeError) as error:
            log('configuration_reload_rejected', reason=str(error))
            return None
        old = {line.id: line for line in self.cfg.lines}
        recheck = set()
        for line in new.lines:
            # 重新启用或更换期望出口时，先关闭线路，不能沿用停用前的探测结果。
            if (line.enabled and not old[line.id].enabled or
                    line.expected_exit != old[line.id].expected_exit or new.exit_policy != self.cfg.exit_policy):
                row = self.health.rows[line.id]
                row['healthy'] = False
                row['reason'] = 'configuration_pending_probe'
                recheck.add(line.id)
        self.cfg = self.health.cfg = new
        # 校验失败保留旧配置；实际发布失败则交由主循环停止服务，不能谎报已回滚。
        self.publish()
        log('configuration_reloaded')
        return recheck

    def run(self):
        import fcntl
        if os.geteuid() != 0:
            raise RuntimeError('内核接口管理需要 root；代理子进程会使用单独账户')
        os.umask(0o077)
        owned_directory(self.cfg.state_dir, 'warp-pool-next:' + self.cfg.instance)
        self.lockfile = (self.cfg.state_dir / 'run.lock').open('a+')
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pins_file = self.cfg.state_dir / 'pins.json'
        self.health = Health(self.cfg, json.loads(pins_file.read_text()) if pins_file.exists() else None)
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGHUP, lambda *_: self.reload_requested.set())
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.cfg.probe_workers,
                                                          thread_name_prefix='warp-health')
        pending = {}
        epochs = {line.id: 0 for line in self.cfg.lines}
        due = {n.id: time.monotonic() for n in self.cfg.lines}
        index = {n.id: i for i, n in enumerate(self.cfg.lines)}
        first_completed = set()
        restart_times = []
        last_publish = 0
        next_repair = 0
        dirty = True
        try:
            self.network.prepare()
            self.proxy = Proxy(self.cfg)
            self.publish()
            log('service_started', lines=len(self.cfg.lines), mode=self.cfg.mode)
            while not self.stop.is_set():
                now = time.monotonic()
                if self.reload_requested.is_set():
                    self.reload_requested.clear()
                    recheck = self.reload()
                    if recheck is not None:
                        for ident in recheck:
                            epochs[ident] += 1
                            due[ident] = now
                if now >= next_repair:
                    repaired = self.network.repair()
                    if repaired:
                        log('lines_repaired', ids=repaired)
                        for ident in repaired:
                            epochs[ident] += 1
                            self.health.rows[ident]['healthy'] = False
                            self.health.rows[ident]['reason'] = 'repair_pending_probe'
                            due[ident] = now
                        dirty = True
                    next_repair = now + self.cfg.repair_interval
                for future, (ident, epoch) in list(pending.items()):
                    if not future.done():
                        continue
                    del pending[future]
                    if epoch != epochs[ident]:
                        continue
                    line = next(n for n in self.cfg.lines if n.id == ident)
                    try:
                        result = future.result()
                    except Exception:
                        result = {'ok': False, 'reason': 'probe_worker_error'}
                    previous_exit = self.health.pins.get(str(ident))
                    self.health.update(line, result)
                    if result['ok'] and previous_exit and result['exit_ipv6'] != previous_exit:
                        log('exit_ipv6_changed', id=ident, previous=previous_exit, current=result['exit_ipv6'],
                            policy=self.cfg.exit_policy, verified=True)
                    delay = self.cfg.health_interval if result['ok'] else min(30, self.cfg.health_interval)
                    # 首轮快速验证，之后把整池探测摊到一个周期内。
                    if ident not in first_completed and result['ok']:
                        delay += index[ident] * self.cfg.health_interval / len(self.cfg.lines)
                    first_completed.add(ident)
                    due[ident] = now + delay
                    dirty = True
                in_progress = {item[0] for item in pending.values()}
                for line in sorted(self.cfg.lines, key=lambda n: due[n.id]):
                    if len(pending) >= self.cfg.probe_workers:
                        break
                    if line.enabled and line.id not in in_progress and due[line.id] <= now:
                        pending[executor.submit(probe, line, self.cfg.probe_timeout)] = (line.id, epochs[line.id])
                if self.proxy.child.poll() is not None:
                    restart_times = [value for value in restart_times if now - value < 60]
                    restart_times.append(now)
                    if len(restart_times) > 3:
                        raise RuntimeError('代理进程一分钟内连续退出，停止服务以避免重启循环')
                    log('proxy_exited', code=self.proxy.child.returncode)
                    self.publish()
                    dirty = False
                    last_publish = now
                if dirty and now - last_publish >= 3 or now - last_publish >= 30:
                    self.publish()
                    dirty = False
                    last_publish = now
                self.stop.wait(1)
        finally:
            if self.proxy:
                self.proxy.close()
            try:
                self.network.close()
            finally:
                if self.health:
                    atomic_write(self.cfg.state_dir / 'status.json', self.snapshot(running=False))
                executor.shutdown(wait=True, cancel_futures=True)
                if self.lockfile:
                    self.lockfile.close()
            log('service_stopped')

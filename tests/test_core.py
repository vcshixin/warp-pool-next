"""验证固定身份、配置注入边界、去重及网络资源归属。"""

import base64
import json
import os
from pathlib import Path
import tempfile
import socket
import unittest
from unittest.mock import Mock, patch

from warp_pool.config import ConfigError, load_config, proxy_config
from warp_pool.network import Network, resolve_endpoint
from warp_pool.service import Health, Service
from warp_pool.storage import atomic_write


class Configuration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'config.json'
        self.raw = {'instance': 'test', 'state_dir': str(self.root / 'state'), 'mode': 'both', 'port': 60900,
                    'min_unique_exits': 2, 'lines': []}
        for index in [1, 2]:
            profile = self.root / f'profile-{index}.conf'
            content = ('[Interface]\nPrivateKey = ' + base64.b64encode(bytes([index]) * 32).decode() +
                       f'\nAddress = 172.16.0.2/32\nAddress = 2606:4700:110::{index}/128\n[Peer]\nPublicKey = ' +
                       base64.b64encode(bytes([3]) * 32).decode() + '\nEndpoint = 162.159.192.1:2408\nAllowedIPs = 0.0.0.0/0,::/0\n')
            profile.write_text(content)
            profile.chmod(0o600)
            secret = self.root / f'password-{index}'
            secret.write_text('safe-password-' + str(index) * 16)
            secret.chmod(0o600)
            self.raw['lines'].append({'id': index, 'username': f'line{index}', 'password_file': str(secret),
                                      'profile': str(profile), 'port': 61000 + index})

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.raw))
        self.path.chmod(0o600)
        return load_config(self.path)

    def test_valid_both_modes(self):
        cfg = self.load()
        output = proxy_config(cfg, {1, 2})
        self.assertEqual(output.count('\nsocks '), 3)
        self.assertIn('parent 1000 extip 2606:4700:110::1 0', output)
        self.assertEqual(output.count('deny * * 0.0.0.0/0,::ffff:0:0/96'), 3)
        self.assertTrue(all(len(cfg.interface(n)) <= 15 for n in cfg.lines))

    def test_unknown_and_unavailable_are_denied(self):
        cfg = self.load()
        output = proxy_config(cfg, {2})
        self.assertNotIn('allow line1', output)
        self.assertNotIn('parent 1000 extip 2606:4700:110::1', output)
        self.assertEqual(output.count('\ndeny *\n'), 3)

    def test_username_cannot_inject_config(self):
        for username in ['a\nallow *', '$/etc/passwd', 'a b', 'a:CL:bad', '*', 'a,b']:
            with self.subTest(username=username):
                self.raw['lines'][0]['username'] = username
                with self.assertRaises(ConfigError):
                    self.load()

    def test_duplicate_identity_rejected(self):
        for field in ['id', 'username', 'profile', 'port']:
            with self.subTest(field=field):
                old = self.raw['lines'][1][field]
                self.raw['lines'][1][field] = self.raw['lines'][0][field]
                with self.assertRaises(ConfigError):
                    self.load()
                self.raw['lines'][1][field] = old

    def test_shared_port_collision_rejected(self):
        self.raw['port'] = self.raw['lines'][0]['port']
        with self.assertRaises(ConfigError):
            self.load()

    def test_unknown_field_rejected(self):
        self.raw['prot'] = 1080
        with self.assertRaises(ConfigError):
            self.load()

    def test_mode_users_does_not_require_line_ports(self):
        self.raw['mode'] = 'users'
        for row in self.raw['lines']:
            del row['port']
        cfg = self.load()
        self.assertEqual(proxy_config(cfg, {1, 2}).count('\nsocks '), 1)

    def random_config(self):
        self.raw['mode'] = 'users'
        self.raw['random_users'] = [{'username': 'pool-random',
                                    'password_file': self.raw['lines'][0]['password_file']}]
        return self.load()

    def test_random_user_uses_only_available_lines_on_same_port(self):
        cfg = self.random_config()
        output = proxy_config(cfg, {2})
        random = output.split('allow pool-random * * * CONNECT\n', 1)[1].split('deny *', 1)[0]
        self.assertEqual(random, 'parent 1000 extip 2606:4700:110::2 0\n')
        self.assertEqual(output.count('\nsocks '), 1)

    def test_random_user_never_falls_back_when_pool_empty(self):
        cfg = self.random_config()
        output = proxy_config(cfg, set())
        self.assertNotIn('allow pool-random', output)
        self.assertNotIn('parent ', output)

    def test_random_user_keeps_fixed_users_and_balances_weight(self):
        cfg = self.random_config()
        output = proxy_config(cfg, {1, 2})
        self.assertIn('allow line1 * * * CONNECT\nparent 1000 extip 2606:4700:110::1 0', output)
        random = output.split('allow pool-random * * * CONNECT\n', 1)[1].split('deny *', 1)[0]
        self.assertEqual(sum(int(row.split()[1]) for row in random.splitlines()), 1000)
        self.assertEqual(random.count('parent 500 extip'), 2)

    def test_random_username_cannot_shadow_fixed_line(self):
        self.random_config()
        self.raw['random_users'][0]['username'] = 'line1'
        with self.assertRaises(ConfigError):
            self.load()

    def test_random_user_cannot_inject_configuration(self):
        self.random_config()
        self.raw['random_users'][0]['username'] = 'pool\nallow *'
        with self.assertRaises(ConfigError):
            self.load()

    def test_random_entry_rejects_multiple_port_mode(self):
        self.random_config()
        self.raw['mode'] = 'both'
        with self.assertRaises(ConfigError):
            self.load()

    def test_port_mode_can_keep_one_legacy_credential(self):
        self.raw['mode'] = 'ports'
        self.raw['lines'][1]['username'] = self.raw['lines'][0]['username']
        self.raw['lines'][1]['password_file'] = self.raw['lines'][0]['password_file']
        cfg = self.load()
        output = proxy_config(cfg, {1, 2})
        self.assertEqual(output.count('\nusers '), 1)
        self.assertEqual(output.count('\nsocks '), 2)
        self.assertIn('parent 1000 extip 2606:4700:110::1 0', output)
        self.assertIn('parent 1000 extip 2606:4700:110::2 0', output)

    def test_same_username_cannot_hide_a_password_conflict(self):
        self.raw['mode'] = 'ports'
        self.raw['lines'][1]['username'] = self.raw['lines'][0]['username']
        with self.assertRaises(ConfigError):
            self.load()

    def test_exit_uniqueness_not_account_count(self):
        cfg = self.load()
        health = Health(cfg)
        for line in cfg.lines:
            health.update(line, {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        accepted, duplicates = health.available()
        self.assertEqual(accepted, {1})
        self.assertEqual(duplicates, {2: 1})

    def test_changed_exit_is_quarantined_immediately(self):
        self.raw['exit_policy'] = 'fixed_ip'
        cfg = self.load()
        health = Health(cfg)
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::2'})
        self.assertFalse(health.rows[1]['healthy'])
        self.assertEqual(health.rows[1]['reason'], 'exit_changed')
        self.assertEqual(health.pins['1'], '2606:4700:54::1')

    def test_existing_pin_persists_across_restart(self):
        self.raw['exit_policy'] = 'fixed_ip'
        cfg = self.load()
        health = Health(cfg, {'1': '2606:4700:54::1'})
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::2'})
        self.assertNotIn(1, health.available()[0])

    def test_default_follows_same_line_when_verified_exit_changes(self):
        cfg = self.load()
        self.assertEqual(cfg.exit_policy, 'follow_line')
        health = Health(cfg, {'1': '2606:4700:54::1'})
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::3'})
        self.assertIn(1, health.available()[0])
        self.assertEqual(health.pins['1'], '2606:4700:54::3')
        generated = proxy_config(cfg, health.available()[0])
        self.assertIn('parent 1000 extip ' + cfg.lines[0].profile.source + ' 0', generated)
        self.assertNotIn('parent 1000 extip ' + cfg.lines[1].profile.source + ' 0', generated)

    def test_changed_exit_must_still_be_unique(self):
        cfg = self.load()
        health = Health(cfg)
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        health.update(cfg.lines[1], {'ok': True, 'exit_ipv6': '2606:4700:54::2'})
        health.update(cfg.lines[1], {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        self.assertEqual(health.available(), ({1}, {2: 1}))
        self.assertNotIn('allow line2', proxy_config(cfg, health.available()[0]))

    def test_explicit_expected_exit_overrides_follow_line(self):
        self.raw['lines'][0]['expected_exit'] = '2606:4700:54::1'
        cfg = self.load()
        health = Health(cfg)
        health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::2'})
        self.assertNotIn(1, health.available()[0])

    def test_policy_reload_rechecks_before_releasing_old_quarantine(self):
        self.raw['exit_policy'] = 'fixed_ip'
        cfg = self.load()
        service = Service(cfg)
        service.health = Health(cfg, {'1': '2606:4700:54::1'})
        service.health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::3'})
        service.publish = Mock()
        self.raw['exit_policy'] = 'follow_line'
        self.load()
        self.assertEqual(service.reload(), {1, 2})
        self.assertNotIn(1, service.health.available()[0])
        service.health.update(service.cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::3'})
        self.assertIn(1, service.health.available()[0])

    def test_transient_failure_threshold_then_recovery(self):
        cfg = self.load()
        health = Health(cfg)
        good = {'ok': True, 'exit_ipv6': '2606:4700:54::1'}
        bad = {'ok': False, 'reason': 'test_timeout'}
        health.update(cfg.lines[0], good)
        health.update(cfg.lines[0], bad)
        self.assertTrue(health.rows[1]['healthy'])
        health.update(cfg.lines[0], bad)
        self.assertFalse(health.rows[1]['healthy'])
        health.update(cfg.lines[0], good)
        self.assertTrue(health.rows[1]['healthy'])

    def test_disabled_user_not_in_proxy_rules(self):
        self.raw['lines'][0]['enabled'] = False
        cfg = self.load()
        self.assertNotIn('allow line1', proxy_config(cfg, {1, 2}))

    def test_foreign_interface_not_adopted(self):
        cfg = self.load()
        net = Network(cfg, execute=Mock())
        item = net.records[0]
        snapshot = {'links': {item['interface']: {'ifalias': 'some-other-service'}}, 'rules': [], 'routes': []}
        with self.assertRaisesRegex(RuntimeError, '不属于'):
            net.assert_owned(snapshot, item)
        net.execute.assert_not_called()

    def test_foreign_priority_not_modified(self):
        cfg = self.load()
        net = Network(cfg, execute=Mock())
        item = net.records[0]
        snapshot = {'links': {}, 'routes': [], 'rules': [{'priority': item['priority'], 'src': '::/0', 'table': 254}]}
        with self.assertRaisesRegex(RuntimeError, '其他配置'):
            net.assert_owned(snapshot, item)
        net.execute.assert_not_called()

    def test_foreign_table_not_modified(self):
        cfg = self.load()
        net = Network(cfg, execute=Mock())
        item = net.records[0]
        snapshot = {'links': {}, 'rules': [], 'routes': [{'table': item['table'], 'dst': 'default', 'dev': 'eth0'}]}
        with self.assertRaisesRegex(RuntimeError, '其他配置'):
            net.assert_owned(snapshot, item)
        net.execute.assert_not_called()

    def test_atomic_json_write(self):
        target = self.root / 'private.json'
        atomic_write(target, {'generation': 1})
        atomic_write(target, {'generation': 2})
        self.assertEqual(json.loads(target.read_text()), {'generation': 2})
        self.assertEqual(list(self.root.glob('.write-*')), [])

    def test_endpoint_prefers_verified_ipv4_outer_transport(self):
        answers = [(socket.AF_INET6, socket.SOCK_DGRAM, 17, '', ('2606:4700:d0::1', 2408, 0, 0)),
                   (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('162.159.192.1', 2408))]
        with patch('warp_pool.network.socket.getaddrinfo', return_value=answers):
            self.assertEqual(resolve_endpoint('engage.cloudflareclient.com:2408'), '162.159.192.1:2408')

    def test_explicit_ipv6_endpoint_remains_supported(self):
        answers = [(socket.AF_INET6, socket.SOCK_DGRAM, 17, '', ('2606:4700:d0::1', 2408, 0, 0))]
        with patch('warp_pool.network.socket.getaddrinfo', return_value=answers):
            self.assertEqual(resolve_endpoint('[2606:4700:d0::1]:2408'), '[2606:4700:d0::1]:2408')

    def test_route_repair_preserves_peer_and_blocks_proxy_first(self):
        cfg = self.load()
        execute = Mock(return_value=Mock(returncode=0, stdout='', stderr=''))
        net = Network(cfg, execute=execute)
        net.proxy_uid = 65534
        snapshot = {'links': {}, 'addresses': {}, 'routes': [], 'rules': [], 'peers': {}}
        for i, (line, item) in enumerate(zip(cfg.lines, net.records)):
            snapshot['links'][item['interface']] = {'ifalias': item['alias'], 'flags': [] if i == 0 else ['UP']}
            snapshot['addresses'][item['interface']] = [{'local': item['source']}]
            snapshot['peers'][item['interface']] = [line.profile.public_key]
            snapshot['rules'].append({'priority': item['priority'], 'src': item['source'], 'table': item['table']})
            snapshot['routes'] += [{'table': item['table'], 'dst': 'default', 'type': 'unreachable', 'metric': 32760},
                                   {'table': item['table'], 'dst': 'default', 'dev': item['interface'], 'metric': 10}]
        net.snapshot = Mock(return_value=snapshot)
        # 隔离规则丢失且网卡关闭时，也只能补一条隔离规则。
        net.blocked.add(1)
        self.assertEqual(net.repair(), [1])
        commands = [call.args[0] for call in execute.call_args_list]
        self.assertFalse(any('setconf' in row for row in commands))
        guards = [i for i, row in enumerate(commands) if 'prohibit' in row]
        self.assertEqual(len(guards), 1)
        self.assertLess(guards[0], next(i for i, row in enumerate(commands) if 'up' in row))

    def test_verified_line_is_unblocked_only_after_explicit_publish(self):
        cfg = self.load()
        execute = Mock(return_value=Mock(returncode=0, stdout='[]', stderr=''))
        net = Network(cfg, execute=execute)
        net.proxy_uid = 65534
        net.blocked = {1, 2}
        rows = [{'priority': item['block_priority'], 'src': item['source'], 'action': 'prohibit',
                 'uid_start': 65534, 'uid_end': 65534} for item in net.records]
        execute.return_value.stdout = json.dumps(rows)
        net.set_available({1})
        execute.assert_not_called()
        net.set_available({1}, unblock=True)
        self.assertEqual(net.blocked, {2})
        deletes = [call.args[0] for call in execute.call_args_list if 'del' in call.args[0]]
        self.assertEqual(len(deletes), 1)
        self.assertIn('uidrange', deletes[0])
        self.assertIn(cfg.lines[0].profile.source + '/128', deletes[0])

    def test_guard_will_not_replace_other_user_policy(self):
        cfg = self.load()
        net = Network(cfg, execute=Mock())
        net.proxy_uid = 65534
        item = net.records[0]
        wrong = {'priority': item['block_priority'], 'src': item['source'], 'action': 'prohibit', 'uid_start': 0, 'uid_end': 0}
        with self.assertRaises(RuntimeError):
            net.guard(item, [wrong])
        net.execute.assert_not_called()

    def test_service_cannot_report_ready_without_proxy(self):
        cfg = self.load()
        service = Service(cfg)
        service.health = Health(cfg)
        for line in cfg.lines:
            service.health.update(line, {'ok': True, 'exit_ipv6': f'2606:4700:54::{line.id}'})
        self.assertFalse(service.snapshot()['ready'])
        service.proxy = Mock(child=Mock(pid=1234, poll=Mock(return_value=None)))
        self.assertTrue(service.snapshot()['ready'])
        service.proxy.child.poll.return_value = 1
        self.assertFalse(service.snapshot()['ready'])

    def test_reenabled_line_must_pass_a_fresh_probe(self):
        self.raw['lines'][0]['enabled'] = False
        cfg = self.load()
        service = Service(cfg)
        service.health = Health(cfg)
        service.health.update(cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        service.publish = Mock()
        self.raw['lines'][0]['enabled'] = True
        self.load()
        self.assertEqual(service.reload(), {1})
        self.assertNotIn(1, service.health.available()[0])
        service.health.update(service.cfg.lines[0], {'ok': True, 'exit_ipv6': '2606:4700:54::1'})
        self.assertIn(1, service.health.available()[0])

    def test_invalid_reload_keeps_old_config_without_publishing(self):
        cfg = self.load()
        service = Service(cfg)
        service.health = Health(cfg)
        service.publish = Mock()
        self.path.write_text('{"instance": null}')
        self.assertIsNone(service.reload())
        self.assertIs(service.cfg, cfg)
        service.publish.assert_not_called()

    def test_publish_failure_is_not_reported_as_safe_rejection(self):
        cfg = self.load()
        service = Service(cfg)
        service.health = Health(cfg)
        service.publish = Mock(side_effect=OSError('模拟磁盘故障'))
        with self.assertRaises(OSError):
            service.reload()


if __name__ == '__main__':
    unittest.main()

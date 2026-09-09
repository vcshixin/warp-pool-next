"""验证批量导入不复用身份，也不会覆盖已有配置。"""

import base64
import json
from pathlib import Path
import tempfile
import unittest

from tools.import_profiles import create_pool
from warp_pool.config import ConfigError, load_config


class ImportProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.target = self.root / 'output'
        for index in [1, 2]:
            profile = self.source / f'{index}.conf'
            profile.write_text('[Interface]\nPrivateKey = ' + base64.b64encode(bytes([index]) * 32).decode() +
                               f'\nAddress = 2606:4700:110::{index}/128\n[Peer]\nPublicKey = ' +
                               base64.b64encode(bytes([3]) * 32).decode() + '\nEndpoint = 162.159.192.1:2408\n')
            profile.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_creates_complete_default_users_config(self):
        result = create_pool(self.source, self.target, 'testpool')
        cfg = load_config(self.target / 'config.json')
        self.assertEqual(result['candidate_lines'], 2)
        self.assertEqual(cfg.mode, 'users')
        self.assertEqual(cfg.min_unique_exits, 2)
        self.assertNotEqual(cfg.lines[0].password, cfg.lines[1].password)
        self.assertEqual(len((self.target / 'clients.tsv').read_text().splitlines()), 3)
        self.assertEqual(len(json.loads((self.target / 'import-map.json').read_text())), 2)

    def test_existing_destination_is_not_modified(self):
        self.target.mkdir()
        marker = self.target / 'important.txt'
        marker.write_text('keep')
        with self.assertRaises(ConfigError):
            create_pool(self.source, self.target, 'testpool')
        self.assertEqual(marker.read_text(), 'keep')
        self.assertEqual(list(self.target.iterdir()), [marker])

    def test_duplicate_source_rejected_before_creating_output(self):
        (self.source / '2.conf').write_bytes((self.source / '1.conf').read_bytes())
        with self.assertRaises(ConfigError):
            create_pool(self.source, self.target, 'testpool')
        self.assertFalse(self.target.exists())

    def test_ports_mode_supports_shared_credentials(self):
        secret = self.root / 'shared-password'
        secret.write_text('shared-password-for-port-mode')
        secret.chmod(0o600)
        create_pool(self.source, self.target, 'testpool', mode='ports', shared_password_file=secret)
        cfg = load_config(self.target / 'config.json')
        self.assertEqual(cfg.lines[0].username, cfg.lines[1].username)
        self.assertEqual(cfg.lines[0].password, cfg.lines[1].password)
        self.assertNotEqual(cfg.lines[0].port, cfg.lines[1].port)


if __name__ == '__main__':
    unittest.main()

"""验证容器健康状态不会把陈旧或未就绪状态当作成功。"""

import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from warp_pool.healthcheck import check


class ContainerHealth(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'instance': 'health', 'state_dir': str(self.root),
                                           'listen': '0.0.0.0', 'port': 57603}))
        self.status = {'instance': 'health', 'running': True, 'ready': True,
                       'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       'controller_pid': 10, 'proxy_pid': 11, 'available_lines': 120}

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        (self.root / 'status.json').write_text(json.dumps(self.status))

    def test_healthy_requires_both_processes_and_socket(self):
        self.save()
        with patch('warp_pool.healthcheck.os.kill') as kill, patch('warp_pool.healthcheck.socket.create_connection') as connect:
            self.assertEqual(check(self.config), {'healthy': True, 'available_lines': 120})
            self.assertEqual(kill.call_count, 2)
            connect.assert_called_once_with(('127.0.0.1', 57603), timeout=2)

    def test_stale_status_is_unhealthy(self):
        self.status['utc'] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=3)).isoformat()
        self.save()
        with self.assertRaises(ValueError):
            check(self.config)

    def test_unready_status_is_unhealthy(self):
        self.status['ready'] = False
        self.save()
        with self.assertRaises(ValueError):
            check(self.config)

    def test_dead_proxy_is_unhealthy(self):
        self.save()
        with patch('warp_pool.healthcheck.os.kill', side_effect=[None, ProcessLookupError()]):
            with self.assertRaises(ProcessLookupError):
                check(self.config)

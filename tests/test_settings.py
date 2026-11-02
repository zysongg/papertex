import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import settings as config_store


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='papertex-settings-')
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()
        self.path = self.dir / 'config.json'

    def test_missing_config_is_created_unconfigured_with_private_mode(self):
        cfg = config_store.load(self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(config_store.is_configured(cfg))
        self.assertEqual(cfg['remote_name'], '远端服务器')
        self.assertEqual(cfg['local_library'], '~/PaperLibrary')

    def test_config_keeps_unknown_fields(self):
        self.path.write_text(json.dumps({
            'ssh_alias': 'lab', 'remote_library': '/srv/lib',
            'remote_workshop': '/srv/proj', 'copy_pdf_local': False,
        }), encoding='utf-8')
        cfg = config_store.load(self.path)
        self.assertFalse(cfg['copy_pdf_local'])
        self.assertEqual(cfg['remote_name'], 'lab')

    def test_broken_config_is_not_silently_replaced(self):
        self.path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            config_store.load(self.path)
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{not json')

    def test_alias_and_direct_targets(self):
        alias = config_store.normalize({
            'ssh_alias': 'lab', 'ssh_host': 'stale.example', 'ssh_user': 'old',
            'remote_library': '/srv/lib', 'remote_workshop': '/srv/proj',
        })
        self.assertEqual(config_store.ssh_target(alias), ['lab'])
        self.assertEqual(config_store.host_target(alias), 'lab')
        self.assertEqual(config_store.scp_target(alias), ['lab'])
        self.assertEqual(alias['ssh_host'], '')

        direct = config_store.normalize({
            'ssh_host': '192.0.2.10', 'ssh_user': 'researcher', 'ssh_port': 2222,
            'remote_library': '/srv/lib', 'remote_workshop': '/srv/proj',
        })
        self.assertEqual(config_store.ssh_target(direct),
                         ['-p', '2222', 'researcher@192.0.2.10'])
        self.assertEqual(config_store.scp_target(direct),
                         ['-P', '2222', 'researcher@192.0.2.10'])
        self.assertEqual(config_store.host_target(direct), 'researcher@192.0.2.10')

    def test_invalid_values_are_rejected(self):
        base = {'remote_library': '/srv/lib', 'remote_workshop': '/srv/proj'}
        bad = [
            {'ssh_host': 'lab', 'ssh_user': 'u', 'host': '0.0.0.0', **base},
            {'ssh_host': 'lab', 'ssh_user': 'u', 'port': 0, **base},
            {'ssh_alias': 'lab', 'remote_library': 'srv/lib', 'remote_workshop': '/srv/proj'},
            {'ssh_alias': 'lab', 'remote_library': '/', 'remote_workshop': '/srv/proj'},
            {'ssh_alias': 'lab', 'remote_library': '/srv/../lib', 'remote_workshop': '/srv/proj'},
            {'ssh_alias': 'lab', 'remote_library': '/srv/lib'},
            {'ssh_host': 'lab', 'remote_library': '/srv/lib', 'remote_workshop': '/srv/proj'},
            {'ssh_alias': '-oProxyCommand=x', **base},
            {'ssh_host': '-oProxyCommand=x', 'ssh_user': 'u', **base},
            {'ssh_host': 'lab', 'ssh_user': 'u', 'ssh_port': 70000, **base},
        ]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                config_store.normalize(values)

    def test_key_path_must_exist_for_direct_mode(self):
        values = {
            'ssh_host': 'lab', 'ssh_user': 'u', 'ssh_key': '~/.ssh/does-not-exist-papertex',
            'remote_library': '/srv/lib', 'remote_workshop': '/srv/proj',
        }
        with self.assertRaises(ValueError):
            config_store.normalize(values)

    def test_write_is_atomic_and_refuses_symlink(self):
        cfg = config_store.normalize({}, strict=False)
        config_store.write(self.path, cfg)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['port'], 8817)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

        link = self.dir / 'linked.json'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            config_store.write(link, cfg)

    def test_connection_checks_report_each_step(self):
        values = {
            'ssh_alias': 'lab', 'remote_library': '/srv/lib',
            'remote_workshop': '/srv/proj', 'local_library': str(self.dir / 'library'),
        }
        calls = []

        class Result:
            def __init__(self, code, out='', err=''):
                self.returncode = code
                self.stdout = out
                self.stderr = err

        def fake(cfg, command, timeout=12):
            calls.append(command)
            if command.startswith('printf ok'):
                return Result(0, 'ok')
            if 'command -v' in command:
                return Result(0, 'ok')
            if '/srv/lib' in command:
                return Result(0, 'ok')
            if '/srv/proj' in command:
                return Result(11)
            return Result(1, '', 'unexpected')

        with patch.object(config_store, '_run_ssh', fake):
            result = config_store.test_connection(values)
        self.assertFalse(result['ok'])
        self.assertTrue(result['ssh'])
        checks = {item['name']: item for item in result['checks']}
        self.assertTrue(checks['SSH 连接']['ok'])
        self.assertTrue(checks['远端资料库']['ok'])
        self.assertFalse(checks['远端项目根']['ok'])
        self.assertIn('不可写', checks['远端项目根']['detail'])

    def test_connection_failure_short_circuits_remote_paths(self):
        values = {
            'ssh_alias': 'lab', 'remote_library': '/srv/lib',
            'remote_workshop': '/srv/proj', 'local_library': str(self.dir / 'library'),
        }

        class Result:
            returncode = 255
            stdout = ''
            stderr = 'Permission denied (publickey).'

        with patch.object(config_store, '_run_ssh', lambda *args, **kwargs: Result()):
            result = config_store.test_connection(values)
        self.assertFalse(result['ok'])
        self.assertFalse(result['ssh'])
        self.assertEqual([item['name'] for item in result['checks']],
                         ['本机库', 'SSH 连接'])
        self.assertIn('认证失败', result['checks'][1]['detail'])


if __name__ == '__main__':
    unittest.main()

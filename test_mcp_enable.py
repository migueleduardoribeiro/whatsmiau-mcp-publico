import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from enable_mcp_media import enable_media


class EnableMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'etc/lylahub-panel/mcp.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'instance_id': 'fixture', 'issuer': 'https://fixture.example',
                                           'state_path': '/var/lib/projeto-exemplo/mcp.db', 'clients': {'fixture': {}}}))
        for name in ('etc/nginx/conf.d', 'etc/nginx/snippets', 'var/backups'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.calls = []

    def runner(self, *args):
        self.calls.append(args)

    def test_enables_fixed_proxy_and_hides_credential_in_result(self):
        result = enable_media(self.root, self.runner)
        config = json.loads(self.config.read_text())
        token = config['media_fetch_token']
        self.assertGreaterEqual(len(token), 43)
        self.assertTrue(config['enable_media'])
        self.assertNotIn(token, str(result))
        self.assertIn(token, (self.root / 'etc/nginx/snippets/lylahub-mcp-fetch-auth.conf').read_text())
        self.assertIn('listen 127.0.0.1:8092;', (self.root / 'etc/nginx/conf.d/lylahub-mcp-files.conf').read_text())
        self.assertLess(self.calls.index(('nginx', '-t')), self.calls.index(('systemctl', 'reload', 'nginx')))

    def test_failed_validation_restores_files_without_service_restart(self):
        old_config = self.config.read_bytes()
        proxy = self.root / 'etc/nginx/conf.d/lylahub-mcp-files.conf'
        proxy.write_text('original proxy')
        def fail(*args):
            self.calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            enable_media(self.root, fail)
        self.assertEqual(self.config.read_bytes(), old_config)
        self.assertEqual(proxy.read_text(), 'original proxy')
        self.assertFalse((self.root / 'etc/nginx/snippets/lylahub-mcp-fetch-auth.conf').exists())
        self.assertEqual(self.calls, [('nginx', '-t')])

    def test_failed_restart_restores_previous_config_and_service(self):
        old_config = self.config.read_bytes()
        failed = False
        def fail_once(*args):
            nonlocal failed
            self.calls.append(args)
            if args == ('systemctl', 'restart', 'lylahub-panel') and not failed:
                failed = True
                raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            enable_media(self.root, fail_once)
        self.assertEqual(self.config.read_bytes(), old_config)
        self.assertEqual(self.calls.count(('systemctl', 'restart', 'lylahub-panel')), 2)

    def test_missing_config_and_symlinks_are_rejected_before_changes(self):
        self.config.unlink()
        with self.assertRaises(ValueError):
            enable_media(self.root, self.runner)
        self.assertEqual(self.calls, [])

    def test_media_can_activate_before_any_oauth_client_is_registered(self):
        config = json.loads(self.config.read_text())
        config['clients'] = {}
        self.config.write_text(json.dumps(config))
        self.assertTrue(enable_media(self.root, self.runner)['enabled'])
        self.assertEqual(json.loads(self.config.read_text())['clients'], {})

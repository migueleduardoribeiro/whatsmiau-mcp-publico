import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from mcp_archive_deploy import patch_archive_nginx, drive_proxy_config
from enable_mcp_archive import enable_archive
from mcp_drive_oauth import CALLBACK


class ArchiveDeployTests(unittest.TestCase):
    def test_patch_is_narrow_idempotent_and_private(self):
        original = Path('nginx-https.conf').read_text()
        patched = patch_archive_nginx(original, docker_subnet='172.17.0.0/16')
        self.assertEqual(patch_archive_nginx(patched, docker_subnet='172.17.0.0/16'), patched)
        for start, end in (('# BEGIN LYLAHUB DRIVE', '# END LYLAHUB DRIVE'),
                           ('# BEGIN LYLAHUB APP INFO', '# END LYLAHUB APP INFO'),
                           ('# BEGIN LYLAHUB ARCHIVE HOOK', '# END LYLAHUB ARCHIVE HOOK')):
            block = patched[patched.index(start):patched.index(end) + len(end)]
            patched = patched.replace(block + '\n    ', '', 1)
        self.assertEqual(patched, original)
        self.assertIn('allow 172.17.0.0/16;', patch_archive_nginx(original, docker_subnet='172.17.0.0/16'))
        with self.assertRaises(ValueError): patch_archive_nginx(original, docker_subnet='0.0.0.0/0')
        with self.assertRaises(ValueError): patch_archive_nginx(original + '\n# BEGIN LYLAHUB DRIVE', docker_subnet='172.17.0.0/16')
    def test_public_information_is_static_and_separate_from_private_status(self):
        from deploy import RELEASE_FILES
        patched = patch_archive_nginx(Path('nginx-https.conf').read_text(), docker_subnet='172.29.0.0/16')
        for name in ('info', 'privacy'):
            self.assertIn('location = /drive/' + name + ' {', patched)
            self.assertIn('static/drive-' + name + '.html', RELEASE_FILES)
    def test_relay_has_only_preexisting_fixed_target(self):
        proxy = drive_proxy_config(relay_instance='personal')
        self.assertIn('location = /relay/personal', proxy)
        self.assertIn('proxy_pass http://192.0.2.10:8081/webhooks/personal;', proxy)
        with self.assertRaises(ValueError): drive_proxy_config(relay_instance='../anything')
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ('etc/lylahub-panel', 'etc/nginx/sites-available', 'etc/nginx/conf.d', 'etc/nginx/snippets', 'var/backups'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.config = self.root / 'etc/lylahub-panel/mcp.json'
        self.config.write_text(json.dumps({'instance_id': 'personal', 'enable_media': True, 'clients': {'existing': {}}}))
        (self.root / 'etc/lylahub-panel/google-drive-client.json').write_text(json.dumps({'web': {
            'client_id': 'fixture.apps.googleusercontent.com', 'client_secret': 'fixture', 'redirect_uris': [CALLBACK]}}))
        self.site = self.root / 'etc/nginx/sites-available/lylahub'
        self.site.write_text(Path('nginx-https.conf').read_text())
    def test_enable_stays_disabled_until_grant_and_preserves_clients(self):
        result = enable_archive(self.root, runner=lambda *args: None, owner_email='owner@example.test', docker_subnet='172.17.0.0/16')
        config = json.loads(self.config.read_text())
        self.assertFalse(config['archive']['capture_enabled'])
        self.assertEqual(config['clients'], {'existing': {}})
        self.assertNotIn(config['archive']['hook_token'], str(result))
        self.assertFalse((self.root / 'var/lib/lylahub-panel/mcp.db').exists())
    def test_validation_failure_restores_every_file(self):
        originals = self.config.read_bytes(), self.site.read_bytes()
        def fail(*args): raise subprocess.CalledProcessError(1, args)
        with self.assertRaises(subprocess.CalledProcessError):
            enable_archive(self.root, runner=fail, owner_email='owner@example.test', docker_subnet='172.17.0.0/16')
        self.assertEqual((self.config.read_bytes(), self.site.read_bytes()), originals)
        self.assertFalse((self.root / 'etc/nginx/conf.d/lylahub-mcp-drive.conf').exists())


if __name__ == '__main__': unittest.main()

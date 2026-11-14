import unittest
from pathlib import Path
from deploy import patch_nginx


class DeploymentTests(unittest.TestCase):
    def test_patch_preserves_everything_outside_panel_api(self):
        text = Path('nginx-https.conf').read_text()
        start = text.index('location ^~ /painel/api/ {')
        end = text.index('}', start)
        patched = patch_nginx(text)
        self.assertEqual(text[:start], patched[:start])
        self.assertEqual(text[end:], patched[patched.index('}', start):])
        self.assertEqual(patched.count('proxy_set_header X-LylaHub-User $remote_user;'), 1)
        self.assertEqual(patch_nginx(patched), patched)

    def test_unexpected_config_aborts_before_modification(self):
        for text in ('', 'location ^~ /painel/api/ { proxy_pass http://other; }'):
            with self.assertRaises(ValueError):
                patch_nginx(text)


if __name__ == '__main__':
    unittest.main()

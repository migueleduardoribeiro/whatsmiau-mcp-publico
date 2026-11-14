import unittest
from pathlib import Path
from mcp_deploy import patch_mcp_nginx, patch_mcp_unit


class MCPDeployTests(unittest.TestCase):
    def test_nginx_changes_only_target_server_and_is_idempotent(self):
        original = Path('nginx-https.conf').read_text()
        patched = patch_mcp_nginx(original)
        self.assertEqual(patch_mcp_nginx(patched), patched)
        self.assertEqual(patched.count('# BEGIN LYLAHUB MCP'), 1)
        added = patched[patched.index('# BEGIN LYLAHUB MCP'):patched.index('# END LYLAHUB MCP') + len('# END LYLAHUB MCP')]
        self.assertEqual(patched.replace(added + '\n    ', ''), original)
        self.assertIn('proxy_set_header Authorization $http_authorization;', added)
        self.assertIn('proxy_set_header X-LylaHub-MCP-Login "";', added)
        self.assertIn('auth_basic_user_file /etc/nginx/exemplo.htpasswd;', added)
        self.assertIn('proxy_request_buffering on;', added)
        self.assertIn('proxy_read_timeout 75s;', added)

    def test_unit_preserves_network_restrictions_and_resource_caps(self):
        original = Path('lylahub-panel.service').read_text()
        patched = patch_mcp_unit(original)
        self.assertEqual(patch_mcp_unit(patched), patched)
        self.assertIn('IPAddressAllow=localhost', patched)
        self.assertIn('MemoryMax=160M', patched)
        self.assertIn('StateDirectory=lylahub-panel', patched)
        self.assertIn('EnvironmentFile=-/etc/projeto-exemplo/mcp.env', patched)

    def test_unexpected_config_aborts(self):
        for value in ('', 'server { server_name other; }'):
            with self.assertRaises(ValueError):
                patch_mcp_nginx(value)
        with self.assertRaises(ValueError):
            patch_mcp_unit('[Service]\nExecStart=other\n')

    def test_memory_migration_accepts_only_known_limits(self):
        original = Path('lylahub-panel.service').read_text().replace('MemoryMax=160M', 'MemoryMax=96M')
        migrated = patch_mcp_unit(original)
        self.assertIn('MemoryMax=160M', migrated)
        self.assertEqual(patch_mcp_unit(migrated), migrated)
        with self.assertRaises(ValueError):
            patch_mcp_unit(original.replace('MemoryMax=96M', 'MemoryMax=200M'))

    def test_updated_release_includes_mcp_modules_and_preserves_old_rollback(self):
        from deploy import RELEASE_FILES
        for name in ('mcp_oauth.py', 'mcp_store.py', 'mcp_protocol.py', 'mcp_tools.py', 'mcp_files.py', 'mcp_contracts.py',
                     'mcp_conversation_models.py','mcp_conversation_source.py','mcp_conversation_store.py',
                     'mcp_conversation_catalog.py','mcp_conversation_query.py','mcp_read_coordinator.py'):
            self.assertIn(name, RELEASE_FILES)

    def test_consent_nginx_policy_and_exact_previous_config_upgrade(self):
        from mcp_deploy import nginx_locations
        current = nginx_locations()
        authorize = current.split('location = /oauth/authorize {', 1)[1].split('}', 1)[0]
        self.assertIn('add_header Referrer-Policy same-origin always;', authorize)
        self.assertNotIn('Referrer-Policy no-referrer', authorize)
        previous = current.replace('Referrer-Policy same-origin always;', 'Referrer-Policy no-referrer always;')
        original = patch_mcp_nginx(Path('nginx-https.conf').read_text())
        old_site = original.replace(current, previous)
        self.assertEqual(patch_mcp_nginx(old_site), original)
        with self.assertRaises(ValueError):
            patch_mcp_nginx(old_site.replace('proxy_set_header Host $host;', 'proxy_set_header Host evil;', 1))

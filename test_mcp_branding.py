import json
import unittest
from unittest.mock import Mock
from mcp_protocol import MCPApplication
from mcp_deploy import nginx_locations,patch_mcp_nginx
from deploy import RELEASE_FILES


class BrandingTests(unittest.TestCase):
    def test_initialize_advertises_public_png(self):
        oauth=Mock(); oauth.issuer='https://fixture.example'
        app=MCPApplication(oauth,Mock())
        _,_,reply=app.rpc(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-11-25','clientInfo':{'name':'test','version':'1'}}}).encode(),Mock(),{})
        self.assertEqual(reply['result']['serverInfo']['icons'],[{'src':'https://fixture.example/mcp-icon.png',
            'mimeType':'image/png','sizes':['512x512']}])
        self.assertIn('static/whatsmiau-icon.png',RELEASE_FILES)
    def test_public_icon_is_static_and_legacy_gateway_is_upgradeable(self):
        content=nginx_locations()
        self.assertIn('location = /mcp-icon.png',content)
        self.assertIn('alias /opt/projeto-exemplo/panel/static/whatsmiau-icon.png;',content)
        marker='    location = /mcp-icon.png'
        legacy=content[:content.index(marker)]+'    # END LYLAHUB MCP'
        self.assertEqual(patch_mcp_nginx(legacy),content)
        self.assertEqual(patch_mcp_nginx(content),content)

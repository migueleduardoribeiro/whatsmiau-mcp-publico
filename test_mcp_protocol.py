import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
from mcp_protocol import MCPApplication
from mcp_store import Store
from mcp_oauth import OAuth
from mcp_tools import Tools


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.oauth = OAuth(self.store, 'https://fixture.example', 'https://fixture.example/mcp',
                           {'client': {'redirect_uris': ['https://chatgpt.com/connector/oauth/callback']}},
                           'fixed', 'owner')
        self.verifier = 'v' * 43
        self.auth = {'client_id': 'client', 'redirect_uri': 'https://chatgpt.com/connector/oauth/callback',
                     'response_type': 'code', 'resource': self.oauth.resource, 'scope': 'whatsapp.read whatsapp.send',
                     'state': 'fixture', 'code_challenge_method': 'S256', 'code_challenge':
                     base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')}
        code = self.oauth.authorize('owner', self.auth)['code']
        self.token = self.oauth.exchange(dict(grant_type='authorization_code', code=code, client_id='client',
                                              redirect_uri=self.auth['redirect_uri'], code_verifier=self.verifier,
                                              resource=self.oauth.resource))['access_token']
        class Bridge:
            def call(self, *args, **kwargs):
                return {'state': 'open'}
        self.tools = Tools(Bridge(), self.store, {'instance_id': 'fixed'})
        self.app = MCPApplication(self.oauth, self.tools)
        self.headers = {'Host': 'fixture.example', 'Authorization': 'Bearer ' + self.token,
                        'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}

    def request(self, obj, **headers):
        return self.app.handle('POST', '/mcp', dict(self.headers, **headers), json.dumps(obj).encode())

    def test_initialize_negotiates_version_and_preserves_id(self):
        status, _, body = self.request({'jsonrpc': '2.0', 'id': 'id-1', 'method': 'initialize',
                                       'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                                  'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual(status, 200)
        self.assertEqual(body['id'], 'id-1')
        self.assertEqual(body['result']['protocolVersion'], '2025-06-18')
        self.assertIn('tools', body['result']['capabilities'])

    def test_notification_202_ping_and_tool_call(self):
        self.assertEqual(self.request({'jsonrpc': '2.0', 'method': 'notifications/initialized'})[0], 202)
        self.assertEqual(self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})[2]['result'], {})
        body = self.request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                             'params': {'name': 'consultar_conexao', 'arguments': {}}})[2]
        self.assertEqual(body['result']['structuredContent']['state'], 'open')

    def test_missing_token_does_not_invoke_tools_and_has_discovery(self):
        self.tools.list_tools = lambda: self.fail('unauthorized tool access')
        status, headers, _ = self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, Authorization='')
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata=', headers['WWW-Authenticate'])

    def test_schema_unknown_method_batch_and_json_errors(self):
        for obj, code in [([], -32600), ({'jsonrpc': '1.0', 'id': 1, 'method': 'ping'}, -32600),
                          ({'jsonrpc': '2.0', 'id': 1, 'method': 'unknown'}, -32601),
                          ({'jsonrpc': '2.0', 'id': True, 'method': 'ping'}, -32600),
                          ({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': []}, -32602)]:
            with self.subTest(obj=obj):
                self.assertEqual(self.request(obj)[2]['error']['code'], code)
        malformed = self.app.handle('POST', '/mcp', self.headers, b'{')
        self.assertEqual(malformed[2]['error']['code'], -32700)

    def test_unknown_tool_and_extra_params_are_not_called(self):
        for params in ({'name': 'delete_instance', 'arguments': {}},
                       {'name': 'consultar_conexao', 'arguments': {}, 'instance_id': 'other'}):
            body = self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': params})[2]
            self.assertEqual(body['error']['code'], -32602)

    def test_host_origin_content_and_body_size(self):
        for change, status in [({'Host': 'evil.example'}, 403), ({'Origin': 'https://evil.example'}, 403),
                               ({'Content-Type': 'text/plain'}, 415), ({'Accept': 'text/html'}, 406),
                               ({'MCP-Protocol-Version': 'unsupported'}, 400)]:
            with self.subTest(change=change):
                self.assertEqual(self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, **change)[0], status)
        self.assertEqual(self.app.handle('POST', '/mcp', self.headers, b'x' * 32769)[0], 413)

    def test_get_stream_not_supported_and_no_cache(self):
        status, headers, _ = self.app.handle('GET', '/mcp', self.headers, b'')
        self.assertEqual(status, 405)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_public_metadata_and_fake_user_header(self):
        status, _, body = self.app.handle('GET', '/.well-known/oauth-protected-resource/mcp',
                                         {'Host': 'fixture.example'}, b'')
        self.assertEqual(status, 200)
        self.assertEqual(body['resource'], self.oauth.resource)
        status, _, _ = self.app.handle('GET', '/oauth/authorize?' + urlencode(self.auth),
                                      {'Host': 'fixture.example', 'X-LylaHub-User': 'owner'}, b'')
        self.assertEqual(status, 403)

    def test_oauth_consent_requires_origin_and_csrf(self):
        headers = {'Host': 'fixture.example', 'X-LylaHub-MCP-Login': '1', 'X-LylaHub-User': 'owner'}
        status, _, body = self.app.handle('GET', '/oauth/authorize?' + urlencode(self.auth), headers, b'')
        self.assertEqual(status, 200)
        self.assertIn(b'whatsapp.send', body)
        for change in ({}, {'Origin': 'https://evil.example'}):
            request_headers = dict(headers, **change, **{'Content-Type': 'application/x-www-form-urlencoded'})
            self.assertEqual(self.app.handle('POST', '/oauth/authorize', request_headers, b'csrf=invalid')[0], 403)

    def test_mcp_two_slots_leave_panel_capacity(self):
        self.assertTrue(self.app.slots.acquire(False))
        self.assertTrue(self.app.slots.acquire(False))
        try:
            self.assertEqual(self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})[0], 429)
        finally:
            self.app.slots.release()
            self.app.slots.release()

    def test_tools_have_output_schema_and_oauth_metadata(self):
        for tool in self.tools.list_tools():
            self.assertIn('outputSchema', tool)
            self.assertEqual(tool['_meta']['securitySchemes'], tool['securitySchemes'])

    def test_full_oauth_form_exchange_and_revocation(self):
        import re
        headers = {'Host': 'fixture.example', 'X-LylaHub-MCP-Login': '1', 'X-LylaHub-User': 'owner'}
        _, _, page = self.app.handle('GET', '/oauth/authorize?' + urlencode(self.auth), headers, b'')
        ticket = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        headers.update({'Origin': self.oauth.issuer, 'Content-Type': 'application/x-www-form-urlencoded'})
        status, redirect, _ = self.app.handle('POST', '/oauth/authorize', headers, urlencode({'csrf': ticket}).encode())
        self.assertEqual(status, 303)
        code = parse_qs(urlsplit(redirect['Location']).query)['code'][0]
        token_headers = {'Host': 'fixture.example', 'Content-Type': 'application/x-www-form-urlencoded'}
        params = dict(grant_type='authorization_code', code=code, client_id='client',
                      redirect_uri=self.auth['redirect_uri'], code_verifier=self.verifier, resource=self.oauth.resource)
        status, _, tokens = self.app.handle('POST', '/oauth/token', token_headers, urlencode(params).encode())
        self.assertEqual(status, 200)
        status, _, _ = self.app.handle('POST', '/oauth/revoke', token_headers,
                                       urlencode({'client_id': 'client', 'resource': self.oauth.resource,
                                                  'token': tokens['access_token']}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(self.request({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                      Authorization='Bearer ' + tokens['access_token'])[0], 401)

    def test_duplicate_json_and_form_parameters_are_rejected(self):
        raw = b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/list"}'
        self.assertEqual(self.app.handle('POST', '/mcp', self.headers, raw)[2]['error']['code'], -32700)
        status, _, _ = self.app.handle('POST', '/oauth/token',
                                       {'Host': 'fixture.example', 'Content-Type': 'application/x-www-form-urlencoded'},
                                       b'client_id=client&client_id=other')
        self.assertEqual(status, 400)

    def test_consent_form_preserves_same_origin_for_browser_post(self):
        headers = {'Host': 'fixture.example', 'X-LylaHub-MCP-Login': '1', 'X-LylaHub-User': 'owner'}
        status, response, _ = self.app.handle('GET', '/oauth/authorize?' + urlencode(self.auth), headers, b'')
        self.assertEqual(status, 200)
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        status, response, _ = self.app.handle('GET', '/.well-known/oauth-authorization-server',
                                               {'Host': 'fixture.example'}, b'')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        headers.update({'Origin': 'null', 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(self.app.handle('POST', '/oauth/authorize', headers, b'csrf=invalid')[0], 403)

    def test_consent_policy_allows_only_self_and_validated_callback_path(self):
        headers = {'Host': 'fixture.example', 'X-LylaHub-MCP-Login': '1', 'X-LylaHub-User': 'owner'}
        status, response, _ = self.app.handle('GET', '/oauth/authorize?' + urlencode(self.auth), headers, b'')
        self.assertEqual(status, 200)
        self.assertIn("form-action 'self' https://chatgpt.com/connector/oauth/callback;", response['Content-Security-Policy'])
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])
        self.assertNotIn('*', response['Content-Security-Policy'])
        invalid = dict(self.auth, redirect_uri='https://evil.example/callback')
        status, response, _ = self.app.handle('GET', '/oauth/authorize?' + urlencode(invalid), headers, b'')
        self.assertEqual(status, 400)
        self.assertNotIn('Content-Security-Policy', response)

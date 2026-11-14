import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from server import make_server
from unittest.mock import Mock


class MCPHTTPTests(unittest.TestCase):
    def test_archive_routes_have_separate_framing_and_dispatch(self):
        server = self.start()
        server.archive_http = Mock()
        server.archive_http.handle.return_value = (200, {'Content-Type': 'application/json'}, {'ok': True})
        headers = {'X-LylaHub-Archive-Hook': '1', 'Content-Type': 'application/json'}
        status, _, _ = self.request(server, '/archive/events/fixed', 'POST', b'x' * 40000, headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(server.archive_http.handle.call_args.args[-1]), 40000)
        server.archive_http.handle.reset_mock()
        self.assertEqual(self.request(server, '/drive/status', headers={'Host': 'evil.test'})[0], 400)
        server.archive_http.handle.assert_not_called()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(temp.cleanup)
        self.config = {'issuer': 'https://fixture.example', 'instance_id': 'fixed', 'allowed_user': 'owner',
                       'clients': {'client': {'redirect_uris': ['https://chatgpt.com/connector/oauth/callback']}},
                       'state_path': str(Path(temp.name) / 'state.db')}

    def start(self, enabled=True):
        server = make_server(('127.0.0.1', 0), 'fixture-secret', mcp_config=self.config if enabled else None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            if server.mcp:
                server.mcp.oauth.store.close()
        self.addCleanup(stop)
        return server

    def request(self, server, path, method='GET', data=None, headers=None):
        hdr = {'Host': 'fixture.example', **(headers or {})}
        request = urllib.request.Request('http://127.0.0.1:' + str(server.server_port) + path,
                                         method=method, data=data, headers=hdr)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_disabled_mcp_and_existing_panel_auth(self):
        server = self.start(False)
        self.assertEqual(self.request(server, '/mcp')[0], 404)
        self.assertEqual(self.request(server, '/instances')[0], 403)

    def test_metadata_and_unauthorized_mcp(self):
        server = self.start()
        status, _, body = self.request(server, '/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['resource'], 'https://fixture.example/mcp')
        status, headers, _ = self.request(server, '/mcp', 'POST', b'{}', {'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertIn('WWW-Authenticate', headers)

    def test_body_and_transfer_encoding_limits(self):
        server = self.start()
        self.assertEqual(self.request(server, '/mcp', 'POST', b'x' * 32769)[0], 413)
        self.assertEqual(self.request(server, '/mcp', 'POST', None, {'Transfer-Encoding': 'chunked'})[0], 400)

    def test_server_disables_mcp_for_missing_required_config(self):
        for key in ('allowed_user', 'instance_id', 'clients'):
            invalid = dict(self.config)
            invalid.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                server = make_server(('127.0.0.1', 0), 'fixture', mcp_config=invalid)
                server.server_close()

    def test_small_rejected_oversize_body_is_drained(self):
        import io
        from email.message import Message
        from types import SimpleNamespace
        from unittest.mock import Mock
        from server import Handler
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(mcp=object())
        handler.headers = Message()
        handler.headers['Host'] = 'fixture.example'
        handler.headers['Content-Length'] = '32769'
        handler.rfile = io.BytesIO(b'x' * 32769)
        handler.connection = Mock()
        handler.reply = Mock()
        handler.dispatch_mcp()
        self.assertEqual(handler.rfile.tell(), 32769)
        self.assertEqual(handler.reply.call_args.args[0], 413)

    def test_media_tools_enabled_only_by_explicit_proxy_config(self):
        self.config['enable_media'] = True
        with self.assertRaises(ValueError):
            self.start()
        self.config['media_fetch_token'] = 'fixture-proxy-token'
        server = self.start()
        tools = {tool['name'] for tool in server.mcp.tools.list_tools()}
        self.assertIn('enviar_documento', tools)
        self.assertIn('enviar_imagem', tools)
        self.assertIn('consultar_imagem', tools)

    def test_pending_callback_exposes_metadata_but_authorizes_no_clients(self):
        self.config['clients'] = {}
        server = self.start()
        self.assertEqual(self.request(server, '/.well-known/oauth-authorization-server')[0], 200)
        from mcp_oauth import AuthError
        with self.assertRaises(AuthError):
            server.mcp.oauth.authorize('owner', {'client_id': 'any', 'redirect_uri': 'https://evil.example/callback'})
        self.assertEqual(self.request(server, '/mcp', 'POST', b'{}')[0], 401)

    def test_legacy_rejected_payload_is_bounded_and_drained(self):
        import io
        from email.message import Message
        from types import SimpleNamespace
        from unittest.mock import Mock
        from server import Handler
        handler = Handler.__new__(Handler)
        handler.path, handler.command = '/instances', 'POST'
        handler.server = SimpleNamespace(origin='https://fixture.example')
        handler.headers = Message()
        for name, value in {'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'owner',
                            'Origin': 'https://fixture.example', 'Content-Type': 'application/json', 'Content-Length': '5000'}.items():
            handler.headers[name] = value
        handler.rfile = io.BytesIO(b'x' * 5000)
        handler.connection = Mock()
        handler.reply = Mock()
        handler.dispatch()
        self.assertEqual(handler.reply.call_args.args[0], 413)
        self.assertEqual(handler.rfile.tell(), 5000)

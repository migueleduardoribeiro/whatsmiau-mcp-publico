"""HTTP behavior tests. Upstream fixture replaces the external WhatsApp API only."""
import importlib
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ORIGIN = 'https://whatsmiau.example.org'
SECRET = 'fixture-private-key'
PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM9sAAAAASUVORK5CYII='


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if self.path == '/capture':
            self.server.captured.append(self.headers.get('apikey'))
            return self.reply(200, [])
        if self.headers.get('apikey') != SECRET:
            return self.reply(401, {'message': 'bad key'})
        if self.server.delay:
            time.sleep(self.server.delay)
        if self.server.failure:
            return self.reply(500, {'message': SECRET, 'proxyPassword': 'do-not-leak'})
        if self.server.redirect:
            self.send_response(302)
            self.send_header('Location', self.server.redirect)
            self.end_headers()
            return
        if self.path == '/v1/instance/fetchInstances':
            return self.reply(200, self.server.instances)
        if self.path.startswith('/v1/webhook/find/'):
            identifier = self.path.rsplit('/', 1)[1]
            item = next((i for i in self.server.instances if i['id'] == identifier), None)
            return self.reply(200, {'webhook': item.get('webhook', {})}) if item else self.reply(404, {})
        if self.path.startswith('/v1/instance/connectionState/'):
            return self.reply(200, {'instance': {'state': 'open'}})
        if self.path.startswith('/v1/instance/connect/'):
            return self.reply(200, {'connected': False, 'base64': PNG, 'proxyPassword': SECRET})
        self.reply(404, {})

    def do_POST(self):
        if self.headers.get('apikey') != SECRET:
            return self.reply(401, {})
        if self.server.failure:
            return self.reply(500, {'message': SECRET})
        if self.path.startswith('/v1/webhook/set/'):
            identifier = self.path.rsplit('/', 1)[1]
            item = next((i for i in self.server.instances if i['id'] == identifier), None)
            if not item:
                return self.reply(404, {})
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            item.setdefault('webhook', {}).update(body['webhook'])
            return self.reply(200, {'webhook': item['webhook']})
        if self.path != '/v1/instance/create':
            return self.reply(404, {})
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        instance = {'id': 'test-uuid', 'instanceName': body['instanceName'], 'proxyPassword': SECRET}
        self.server.instances.append(instance)
        self.reply(201, instance)

    def do_DELETE(self):
        if self.headers.get('apikey') != SECRET:
            return self.reply(401, {})
        if self.server.failure:
            return self.reply(500, {'message': SECRET})
        identifier = self.path.rsplit('/', 1)[1]
        item = next((i for i in self.server.instances if i['id'] == identifier), None)
        if not item:
            return self.reply(404, {})
        if self.path.startswith('/v1/instance/logout/'):
            item.pop('ownerJid', None)
            if hasattr(self.server, 'connected'):
                self.server.connected.discard(identifier)
            return self.reply(200, {'message': 'logged out'})
        if self.path.startswith('/v1/instance/delete/'):
            self.server.instances.remove(item)
            return self.reply(200, {'message': 'deleted'})
        return self.reply(404, {})


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.module = importlib.import_module('server')
        except ModuleNotFoundError:
            cls.module = None

    def setUp(self):
        self.assertIsNotNone(self.module, 'Panel HTTP server has not been implemented')
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.instances = [{'id': 'first-uuid', 'instanceName': '<img onerror=alert(1)>',
                                    'ownerJid': '5511999999999@s.whatsapp.net',
                                    'proxyPassword': SECRET, 'webhook': {'token': 'hidden'}}]
        self.upstream.delay = 0
        self.upstream.failure = False
        self.upstream.redirect = None
        self.upstream.captured = []
        self.thread(self.upstream)
        self.server = self.module.make_server(
            ('127.0.0.1', 0), SECRET,
            upstream=f'http://127.0.0.1:{self.upstream.server_port}', origin=ORIGIN, timeout=0.2)
        self.thread(self.server)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def thread(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, path='/instances', method='GET', data=None, headers=None):
        actual = {'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'usuario-exemplo', 'Origin': ORIGIN, 'Content-Type': 'application/json'}
        if headers is not None:
            actual = headers
        payload = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.url + path, data=payload, headers=actual, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read().decode()
            return response.status, json.loads(body), response.headers

    def test_list_includes_live_status_and_filters_credentials(self):
        status, body, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['instances'], [{'id': 'first-uuid', 'name': '<img onerror=alert(1)>',
                                             'number': '5511999999999', 'state': 'open'}])
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn('webhook', json.dumps(body))
        self.assertIn('no-store', headers['Cache-Control'])

    def test_create_reaches_upstream_and_is_visible_in_list(self):
        status, created, _ = self.request(method='POST', data={'name': 'Atendimento'})
        self.assertEqual(status, 201)
        self.assertEqual(created, {'id': 'test-uuid', 'name': 'Atendimento'})
        _, listed, _ = self.request()
        self.assertIn('Atendimento', [i['name'] for i in listed['instances']])
        self.assertNotIn(SECRET, json.dumps(created))

    def test_connect_returns_qr_without_credentials(self):
        status, body, _ = self.request('/instances/first-uuid/connect', 'POST', {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'connected': False, 'base64': PNG})

    def test_cross_origin_and_missing_marker_cannot_mutate(self):
        for headers in ({'Origin': 'https://evil.invalid', 'X-LylaHub-Panel': '1'},
                        {'Origin': ORIGIN}, {'X-LylaHub-Panel': '1'}):
            status, _, _ = self.request(method='POST', data={'name': 'Hacked'}, headers=headers)
            self.assertEqual(status, 403)
        _, listed, _ = self.request()
        self.assertEqual(len(listed['instances']), 1)

    def test_invalid_names_cannot_create(self):
        for name in ('', '  ', 'a' * 81, 'bad\nname', 'bad/path', '<script>'):
            with self.subTest(name=name):
                status, _, _ = self.request(method='POST', data={'name': name})
                self.assertEqual(status, 400)

    def test_arbitrary_api_paths_and_traversal_are_rejected(self):
        for path in ('/messages/send', '/instances/a%2Fb/connect', '/instances?url=http://evil',
                     '/instances/../connect'):
            with self.subTest(path=path):
                status, _, _ = self.request(path, 'POST', {})
                self.assertEqual(status, 404)

    def test_upstream_errors_are_sanitized(self):
        self.upstream.failure = True
        status, body, _ = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn('do-not-leak', json.dumps(body))

    def test_upstream_timeout_is_bounded(self):
        self.upstream.delay = 0.5
        status, _, _ = self.request()
        self.assertEqual(status, 504)

    def test_upstream_redirect_cannot_forward_api_key(self):
        self.upstream.redirect = f'http://127.0.0.1:{self.upstream.server_port}/capture'
        status, _, _ = self.request()
        self.assertEqual(status, 502)
        self.assertEqual(self.upstream.captured, [])

    def test_malformed_and_oversize_payloads_are_rejected(self):
        req = urllib.request.Request(self.url + '/instances', data=b'{', method='POST',
                                     headers={'Origin': ORIGIN, 'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'usuario-exemplo',
                                              'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(req)
        self.assertEqual(result.exception.code, 400)
        result.exception.close()
        status, _, _ = self.request(method='POST', data={'name': 'a' * 5000})
        self.assertEqual(status, 413)


if __name__ == '__main__':
    unittest.main()

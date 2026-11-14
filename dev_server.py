"""Local UI fixture, never deployed. Does not contact WhatsApp or read real secrets."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from server import make_server
from test_server import Upstream, SECRET

ORIGIN = 'http://localhost:8765'


class Fixture(Upstream):
    def do_GET(self):
        if self.path.startswith('/v1/instance/connectionState/'):
            identifier = self.path.rsplit('/', 1)[1]
            return self.reply(200, {'state': 'open' if identifier in self.server.connected else 'close'})
        if self.path.startswith('/v1/instance/connect/'):
            identifier = self.path.rsplit('/', 1)[1]
            if identifier in self.server.connected:
                return self.reply(200, {'connected': True})
        super().do_GET()


class Frontend(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body, kind='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        if self.path.startswith('/__test/') and self.command == 'POST':
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
            if self.path == '/__test/seed':
                upstream.instances = payload.get('instances', [])
                upstream.connected = set(payload.get('connected', []))
            elif self.path == '/__test/failure':
                upstream.failure = payload.get('failure', False)
            return self.reply(200, b'{}')
        if self.path.startswith('/painel/api/'):
            data = self.rfile.read(int(self.headers.get('Content-Length', '0'))) if self.command == 'POST' else None
            req = urllib.request.Request(f'http://127.0.0.1:{bridge.server_port}' + self.path.removeprefix('/painel/api'),
                                         data=data, method=self.command,
                                         headers={'Origin': self.headers.get('Origin', ''),
                                                  'X-LylaHub-Panel': self.headers.get('X-LylaHub-Panel', ''),
                                                  'X-LylaHub-User': 'fixture-user',
                                                  'Content-Type': self.headers.get('Content-Type', '')})
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return self.reply(response.status, response.read())
        files = {'/painel/': ('index.html', 'text/html; charset=utf-8'),
                 '/painel/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/painel/app.css': ('app.css', 'text/css; charset=utf-8')}
        if self.path in files:
            name, kind = files[self.path]
            return self.reply(200, (Path(__file__).parent / 'static' / name).read_bytes(), kind)
        self.reply(404, b'{}')

    do_GET = dispatch
    do_POST = dispatch


if __name__ == '__main__':
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    upstream.instances, upstream.connected, upstream.failure, upstream.delay = [], set(), False, 0
    upstream.redirect, upstream.captured = None, []
    bridge = make_server(('127.0.0.1', 0), SECRET, f'http://127.0.0.1:{upstream.server_port}', origin=ORIGIN)
    for server in (upstream, bridge):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('Local fixture ready at ' + ORIGIN + '/painel/', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), Frontend).serve_forever()

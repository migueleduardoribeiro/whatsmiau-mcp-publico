"""One-shot isolated Nginx auth integration check; never changes real htpasswd.

Run as root on the gateway after deployment. Uses a temporary loopback listener
and fixture password, production panel location and bridge. No instance writes.
"""
import base64
import json
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    site = Path('/etc/nginx/sites-available/lylahub').read_text()
    start = site.index('location ^~ /painel/api/ {')
    end = site.index('}', start) + 1
    location = site[start:end]
    with tempfile.TemporaryDirectory(prefix='panel-auth-check-') as temporary:
        folder = Path(temporary)
        password_file = folder / 'fixture.htpasswd'
        password_file.write_text('usuario-exemplo:{PLAIN}fixture-not-a-real-password\n')
        password_file.chmod(0o600)
        location = location.replace('/etc/nginx/exemplo.htpasswd', str(password_file))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        config = folder / 'nginx.conf'
        config.write_text('daemon off; master_process off; pid ' + str(folder / 'pid') + ';\n'
                          'error_log stderr error; events { worker_connections 16; }\n'
                          'http { access_log off; server { listen 127.0.0.1:' + str(port) + ';\n' + location + '\n} }\n')
        subprocess.run(['nginx', '-t', '-p', str(folder), '-c', str(config)], check=True)
        process = subprocess.Popen(['nginx', '-p', str(folder), '-c', str(config)], stdout=subprocess.DEVNULL)
        try:
            def request(path, method='GET', body=None, auth=True, origin='https://whatsmiau.example.org'):
                headers = {'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'forged-user', 'Content-Type': 'application/json', 'Origin': origin}
                if auth:
                    headers['Authorization'] = 'Basic ' + base64.b64encode(b'usuario-exemplo:fixture-not-a-real-password').decode()
                request = urllib.request.Request('http://127.0.0.1:' + str(port) + '/painel/api' + path, method=method,
                                                 data=None if body is None else json.dumps(body).encode(), headers=headers)
                try:
                    response = urllib.request.urlopen(request, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, response.read(), response.headers
            for attempt in range(20):
                try:
                    status, _, _ = request('/instances', auth=False)
                    break
                except urllib.error.URLError:
                    if process.poll() is not None or attempt == 19:
                        raise
                    time.sleep(.1)
            assert status == 401
            status, raw, headers = request('/instances')
            assert status == 200 and isinstance(json.loads(raw)['instances'], list)
            assert 'no-store' in headers['Cache-Control']
            status, _, _ = request('/credentials/reveal', 'POST', {'confirm': True}, origin='https://evil.invalid')
            assert status == 403
            status, raw, headers = request('/credentials/reveal', 'POST', {'confirm': True})
            assert status == 200 and json.loads(raw)['scope'] == 'global' and 'no-store' in headers['Cache-Control']
            print('NGINX_AUTH_FIXTURE_USER_FORWARDING_CSRF_NO_CACHE_OK')
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()

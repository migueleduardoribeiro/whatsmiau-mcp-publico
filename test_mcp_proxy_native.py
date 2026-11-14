"""Native TLS regression with a local CA chain; never contacts external hosts."""
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from mcp_media import proxy_config


@unittest.skipUnless(os.name != 'nt' and getattr(os, 'geteuid', lambda: -1)() == 0
                     and shutil.which('nginx'), 'Native Linux/root Nginx acceptance')
class NativeProxyTests(unittest.TestCase):
    def test_intermediate_chain_and_signed_path_pass_but_wrong_hostname_fails(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        host = 'oaisdmntprbrazilsouth.blob.core.windows.net'

        def issue(name, issuer=None, ca_depth=None):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            now = datetime.now(timezone.utc)
            builder = (x509.CertificateBuilder().subject_name(subject)
                .issuer_name(issuer[1].subject if issuer else subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=ca_depth is not None, path_length=ca_depth), True))
            if ca_depth is None:
                builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
            return key, builder.sign(issuer[0] if issuer else key, hashes.SHA256())

        root_ca = issue('fixture-root', ca_depth=2)
        first = issue('fixture-intermediate-1', root_ca, 1)
        second = issue('fixture-intermediate-2', first, 0)
        leaf = issue(host, second)
        with tempfile.TemporaryDirectory(dir='.') as name:
            root = Path(name).resolve()
            pem = serialization.Encoding.PEM
            trust = root / 'root.pem'; trust.write_bytes(root_ca[1].public_bytes(pem))
            chain = root / 'chain.pem'
            chain.write_bytes(b''.join(cert.public_bytes(pem) for _, cert in (leaf, second, first)))
            key_path = root / 'leaf.key'
            key_path.write_bytes(leaf[0].private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
            key_path.chmod(0o600)
            requests = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append((self.path, self.headers['Host']))
                    self.send_response(200); self.send_header('Content-Length', '12'); self.end_headers()
                    self.wfile.write(b'%PDF-fixture')
                def log_message(self, *_): pass

            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.addCleanup(upstream.server_close)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(chain, key_path)
            upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
            worker = threading.Thread(target=upstream.serve_forever, daemon=True); worker.start()
            self.addCleanup(worker.join, 2)
            self.addCleanup(upstream.shutdown)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            config = (proxy_config().replace('127.0.0.1:8092', '127.0.0.1:' + str(port))
                .replace('include /etc/nginx/snippets/lylahub-mcp-fetch-auth.conf;',
                         'if ($http_x_lylahub_fetch_token != "fixture-token") { return 403; }')
                .replace('/etc/ssl/certs/ca-certificates.crt', str(trust))
                .replace('https://$http_x_lylahub_fetch_ip$lylahub_mcp_fetch_uri',
                         'https://$http_x_lylahub_fetch_ip:' + str(upstream.server_port) + '$lylahub_mcp_fetch_uri'))
            main = root / 'nginx.conf'
            main.write_text('pid ' + str(root / 'nginx.pid') + ';\nerror_log /dev/null crit;\n'
                            'events {}\nhttp {\n' + config + '\n}\n')
            command = ['nginx', '-c', str(main)]
            started = False
            try:
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                started = True
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                path = '/container/file.pdf?sv=fixture&sig=a%2Bb%2Fc%3D'
                headers = {'X-LylaHub-Fetch-IP': '127.0.0.1', 'X-LylaHub-Fetch-Token': 'fixture-token'}
                request = urllib.request.Request('http://127.0.0.1:' + str(port)
                    + '/openai-blob-brazilsouth' + path, headers=headers)
                with opener.open(request, timeout=5) as response:
                    self.assertEqual(response.read(), b'%PDF-fixture')
                self.assertEqual(requests, [(path, host)])
                # The same trusted chain must fail for another fixed hostname.
                request = urllib.request.Request('http://127.0.0.1:' + str(port) + '/openai/file', headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(request, timeout=5)
                self.assertEqual(error.exception.code, 502)
                error.exception.close()
                self.assertEqual(len(requests), 1)
            finally:
                if started:
                    subprocess.run(command + ['-s', 'quit'], check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

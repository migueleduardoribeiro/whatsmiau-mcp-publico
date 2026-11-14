"""Bounded media transfer through a loopback-only, fixed-host TLS proxy.

The panel retains IPAddressAllow=localhost. It resolves and validates the public
IPv4 destination, then pins that IP through Nginx, which verifies the fixed TLS
hostname. No redirects, arbitrary destinations, archive extraction or disk cache.
"""
import base64
import hashlib
import hmac
import ipaddress
import io
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlsplit
from mcp_contracts import instance_path
from mcp_files import FileError, validate_file_reference

DOWNLOAD_ROUTES = {
    'openai': 'files.oaiusercontent.com',
    'openai-brazilsouth': 'sdmntprbrazilsouth.oaiusercontent.com',
    'openai-blob-brazilsouth': 'oaisdmntprbrazilsouth.blob.core.windows.net',
    'whatsapp': 'mmg.whatsapp.net',
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _key(descriptor, field):
    try:
        raw = descriptor[field]
        if not isinstance(raw, str) or len(raw) > 64:
            raise ValueError()
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) != 32:
            raise ValueError()
        return decoded
    except (KeyError, ValueError):
        raise FileError('invalid_media_descriptor') from None


def decrypt_image(encrypted, descriptor):
    """Authenticate encrypted media before AES-CBC decryption and SHA-256 check."""
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    try:
        length = int(descriptor['fileLength'])
        if not 0 < length <= 5 * 1024 * 1024 or not 10 < len(encrypted) <= 5 * 1024 * 1024 + 26:
            raise ValueError()
        if not hmac.compare_digest(hashlib.sha256(encrypted).digest(), _key(descriptor, 'fileEncSha256')):
            raise ValueError()
        expanded = HKDF(algorithm=hashes.SHA256(), length=112, salt=None,
                        info=b'WhatsApp Image Keys').derive(_key(descriptor, 'mediaKey'))
        iv, aes_key, mac_key = expanded[:16], expanded[16:48], expanded[48:80]
        ciphertext, mac = encrypted[:-10], encrypted[-10:]
        if not hmac.compare_digest(hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()[:10], mac):
            raise ValueError()
        decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        data = unpadder.update(padded) + unpadder.finalize()
        if len(data) != length or not hmac.compare_digest(hashlib.sha256(data).digest(), _key(descriptor, 'fileSha256')):
            raise ValueError()
        return data
    except (KeyError, TypeError, ValueError):
        raise FileError('media_integrity_failed') from None


class MediaProvider:
    allowed_hosts = frozenset(host for route, host in DOWNLOAD_ROUTES.items() if route.startswith('openai'))

    def __init__(self, bridge, config):
        self.bridge, self.config = bridge, config
        if not isinstance(config.get('media_fetch_token'), str) or not config['media_fetch_token']:
            raise ValueError('Media proxy credential required')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.cache, self.lock, self.clock = OrderedDict(), threading.RLock(), time.time

    def _download(self, url, route, maximum):
        output = io.BytesIO()
        self.download_to(url, route, maximum, output)
        return output.getvalue()

    def download_to(self, url, route, maximum, target):
        host = DOWNLOAD_ROUTES.get(route)
        validate_file_reference({'download_url': url, 'file_id': 'internal-reference'}, {host})
        parsed = urlsplit(url)
        try:
            addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            ips = {record[4][0] for record in addresses}
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise FileError('private_media_destination')
            ip = sorted(ips)[0]
            local_url = 'http://127.0.0.1:8092/' + route + parsed.path
            if parsed.query:
                local_url += '?' + parsed.query
            request = urllib.request.Request(local_url, headers={
                'X-LylaHub-Fetch-IP': ip, 'X-LylaHub-Fetch-Token': self.config['media_fetch_token']})
            deadline = time.monotonic() + 30
            with self.opener.open(request, timeout=5) as response:
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise FileError('media_download_failed')
                declared = response.headers.get('Content-Length')
                if declared and (not declared.isdigit() or int(declared) > maximum):
                    raise FileError('invalid_file_size')
                count = 0
                while True:
                    if time.monotonic() > deadline:
                        raise FileError('media_download_timeout')
                    chunk = response.read1(min(65536, maximum + 1 - count))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > maximum:
                        raise FileError('invalid_file_size')
                    target.write(chunk)
                if declared is not None and count != int(declared):
                    raise FileError('media_download_failed')
                return count
        except FileError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            raise FileError('media_download_failed') from None

    def fetch_authorized(self, reference, max_bytes):
        validate_file_reference(reference, self.allowed_hosts)
        host = urlsplit(reference['download_url']).hostname
        route = next(route for route, destination in DOWNLOAD_ROUTES.items() if destination == host)
        return self._download(reference['download_url'], route, max_bytes)

    def deliver(self, *, instance, recipient, data, mime_type, filename, caption, kind):
        if instance != self.config['instance_id'] or kind not in ('document', 'image'):
            raise FileError('access_denied')
        response = self.bridge.call_media(instance_path(instance, 'message/' + kind),
                                          {'number': recipient, 'mimetype': mime_type,
                                           'fileName': filename, 'caption': caption}, data, mime_type)
        return {'message_id': response.get('key', {}).get('id'), 'status': 'accepted'}

    def remember_image(self, instance, chat, message_id, descriptor, timestamp):
        if instance != self.config['instance_id'] or not isinstance(descriptor, dict) or descriptor.get('viewOnce'):
            return False
        try:
            for field in ('mediaKey', 'fileSha256', 'fileEncSha256'):
                _key(descriptor, field)
            if not 0 < int(descriptor.get('fileLength', '0')) <= 5 * 1024 * 1024:
                return False
            validate_file_reference({'download_url': descriptor.get('url'), 'file_id': 'internal-reference'}, {'mmg.whatsapp.net'})
            stored = {field: descriptor[field] for field in ('url', 'mediaKey', 'fileSha256', 'fileEncSha256', 'fileLength')}
            stored.update(mimetype=str(descriptor.get('mimetype', ''))[:128],
                          caption=str(descriptor.get('caption', ''))[:4096], timestamp=int(timestamp))
        except (ValueError, TypeError, KeyError):
            return False
        with self.lock:
            self._expire()
            key = (instance, chat, message_id)
            self.cache[key] = (self.clock() + 900, stored)
            self.cache.move_to_end(key)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        return True

    def _expire(self):
        for key in [key for key, entry in self.cache.items() if entry[0] <= self.clock()]:
            del self.cache[key]

    def read_image(self, instance, chat, message_id):
        with self.lock:
            self._expire()
            entry = self.cache.get((instance, chat, message_id))
            if not entry or instance != self.config['instance_id']:
                raise FileError('image_reference_unavailable')
            descriptor = dict(entry[1])
        encrypted = self._download(descriptor['url'], 'whatsapp', 5 * 1024 * 1024 + 26)
        data = decrypt_image(encrypted, descriptor)
        return {'data': data, 'mime_type': descriptor['mimetype'], 'caption': descriptor['caption'],
                'timestamp': datetime.fromtimestamp(descriptor['timestamp'], timezone.utc).isoformat()}


def proxy_config():
    """Separate optional conf.d file. Not installed by ordinary panel deployment."""
    blocks = ['map $request_uri $lylahub_mcp_fetch_uri {',
              '    ~^/(?:' + '|'.join(DOWNLOAD_ROUTES) + ')(/.*)$ $1;', '    default "";', '}',
              'server {', '    listen 127.0.0.1:8092;', '    server_name _;',
              '    access_log off;', '    error_log /dev/null crit;',
              '    include /etc/nginx/snippets/lylahub-mcp-fetch-auth.conf;',
              '    client_max_body_size 1k;',
              '    if ($http_x_lylahub_fetch_ip !~ "^[0-9]{1,3}(\\.[0-9]{1,3}){3}$") { return 400; }']
    for route, host in DOWNLOAD_ROUTES.items():
        blocks.extend(['    location ^~ /' + route + '/ {', '        limit_except GET { deny all; }',
                       '        proxy_pass https://$http_x_lylahub_fetch_ip$lylahub_mcp_fetch_uri;',
                       '        proxy_set_header Host ' + host + ';', '        proxy_ssl_server_name on;',
                       '        proxy_ssl_name ' + host + ';', '        proxy_ssl_verify on;',
                       '        proxy_ssl_verify_depth 5;',
                       '        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;',
                       '        proxy_pass_request_headers off;', '        proxy_pass_request_body off;',
                       '        proxy_set_header Content-Length "";', '        proxy_set_header Accept-Encoding "";',
                       '        proxy_set_header Origin https://web.whatsapp.com;' if route == 'whatsapp' else '',
                       '        proxy_buffering off;', '        proxy_max_temp_file_size 0;',
                       '        proxy_connect_timeout 3s;', '        proxy_read_timeout 5s;', '    }'])
    blocks.extend(['    location / { return 404; }', '}', ''])
    return '\n'.join(line for line in blocks if line)

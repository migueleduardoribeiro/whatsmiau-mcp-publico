"""Provider tests use synthetic media and simulated HTTP only."""
import base64
import hashlib
import hmac
import io
import unittest
from unittest.mock import Mock, patch
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp_media import MediaProvider, decrypt_image, proxy_config
from mcp_files import FileError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=')


def encrypted_fixture():
    key = b'\x01' * 32
    expanded = HKDF(algorithm=hashes.SHA256(), length=112, salt=None, info=b'WhatsApp Image Keys').derive(key)
    iv, aes, mac = expanded[:16], expanded[16:48], expanded[48:80]
    padder = padding.PKCS7(128).padder()
    padded = padder.update(PNG) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes), modes.CBC(iv)).encryptor()
    body = encryptor.update(padded) + encryptor.finalize()
    body += hmac.new(mac, iv + body, hashlib.sha256).digest()[:10]
    b64 = lambda data: base64.b64encode(data).decode()
    return body, {'url': 'https://mmg.whatsapp.net/fixture.enc?token=fixture', 'mimetype': 'image/png',
                  'mediaKey': b64(key), 'fileSha256': b64(hashlib.sha256(PNG).digest()),
                  'fileEncSha256': b64(hashlib.sha256(body).digest()), 'fileLength': str(len(PNG)),
                  'caption': 'fixture caption'}


class Response(io.BytesIO):
    status = 200
    headers = {}
    def read1(self, size=-1):
        return self.read(size)


class MediaTests(unittest.TestCase):
    def test_chatgpt_azure_blob_host_keeps_signed_query_and_exact_tls_destination(self):
        host = 'oaisdmntprbrazilsouth.blob.core.windows.net'
        reference = {'file_id': 'opaque:generated/document.pdf',
            'download_url': 'https://' + host + '/container/document.pdf?sv=fixture&sig=a%2Bb%2Fc%3D'}
        self.provider.opener.open.return_value = Response(b'%PDF-synthetic')
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('20.209.83.225', 443))]):
            self.assertEqual(self.provider.fetch_authorized(reference, 1024), b'%PDF-synthetic')
        request = self.provider.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
            'http://127.0.0.1:8092/openai-blob-brazilsouth/container/document.pdf?sv=fixture&sig=a%2Bb%2Fc%3D')
        config = proxy_config()
        self.assertIn('location ^~ /openai-blob-brazilsouth/ {', config)
        self.assertIn('proxy_ssl_name ' + host + ';', config)
        self.assertIn('proxy_set_header Host ' + host + ';', config)

    def test_observed_openai_regional_host_uses_its_own_pinned_tls_route(self):
        host = 'sdmntprbrazilsouth.oaiusercontent.com'
        reference = {'file_id': 'opaque:fixture/document.pdf',
            'download_url': 'https://' + host + '/private/file?signature=fixture'}
        self.provider.opener.open.return_value = Response(b'synthetic file')
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]) as dns:
            data = self.provider.fetch_authorized(reference, 1024)
        self.assertEqual(data, b'synthetic file')
        self.assertEqual(dns.call_args.args[0], host)
        request = self.provider.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
            'http://127.0.0.1:8092/openai-brazilsouth/private/file?signature=fixture')
        self.assertEqual(request.get_header('X-lylahub-fetch-ip'), '93.184.216.34')
        config = proxy_config()
        self.assertIn('location ^~ /openai-brazilsouth/ {', config)
        self.assertIn('proxy_set_header Host ' + host + ';', config)
        self.assertIn('proxy_ssl_name ' + host + ';', config)
        self.assertIn('proxy_ssl_verify on;', config)

    def test_regional_support_does_not_allow_unknown_hosts_or_private_ips(self):
        for host in ('unknown.oaiusercontent.com', 'sdmntprbrazilsouth.oaiusercontent.com.evil.example',
                     'oaiusercontent.com', '127.0.0.1', 'other.blob.core.windows.net',
                     'oaisdmntprbrazilsouth.blob.core.windows.net.evil.example'):
            with self.subTest(host=host), self.assertRaises(FileError):
                self.provider.fetch_authorized({'file_id': 'opaque:fixture',
                    'download_url': 'https://' + host + '/file'}, 1024)
        for host in ('sdmntprbrazilsouth.oaiusercontent.com', 'oaisdmntprbrazilsouth.blob.core.windows.net'):
            with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.0.2.10', 443))]):
                with self.assertRaisesRegex(FileError, 'private_media_destination'):
                    self.provider.fetch_authorized({'file_id': 'opaque:fixture',
                        'download_url': 'https://' + host + '/file'}, 1024)
        self.provider.opener.open.assert_not_called()

    def test_download_to_streams_bounded_chunks(self):
        class Target(io.BytesIO):
            def write(self, value):
                if len(value) > 65536: raise AssertionError('unbounded chunk')
                return super().write(value)
        self.provider.opener.open.return_value = Response(b'x' * 200000)
        target = Target()
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            self.provider.download_to(self.reference['download_url'], 'openai', 200000, target)
        self.assertEqual(len(target.getvalue()), 200000)

    def setUp(self):
        self.provider = MediaProvider(Mock(), {'instance_id': 'fixed', 'media_fetch_token': 'fixture-token'})
        self.provider.opener = Mock()
        self.reference = {'download_url': 'https://files.oaiusercontent.com/fixture?signature=fixture',
                          'file_id': 'file-fixture'}

    def test_openai_fetch_uses_loopback_proxy_and_pinned_public_ip(self):
        self.provider.opener.open.return_value = Response(b'opaque file')
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            data = self.provider.fetch_authorized(self.reference, 1024)
        request = self.provider.opener.open.call_args.args[0]
        self.assertEqual(data, b'opaque file')
        self.assertEqual(request.full_url, 'http://127.0.0.1:8092/openai/fixture?signature=fixture')
        self.assertEqual(request.get_header('X-lylahub-fetch-ip'), '93.184.216.34')
        self.assertEqual(request.get_header('X-lylahub-fetch-token'), 'fixture-token')

    def test_private_dns_and_redirects_and_oversize_are_rejected(self):
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.0.2.10', 443))]):
            with self.assertRaises(FileError):
                self.provider.fetch_authorized(self.reference, 1024)
        self.provider.opener.open.assert_not_called()
        for status, size in [(302, 1), (200, 1025)]:
            response = Response(b'x' * size)
            response.status = status
            self.provider.opener.open.return_value = response
            with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
                with self.assertRaises(FileError):
                    self.provider.fetch_authorized(self.reference, 1024)

    def test_documents_are_sent_as_server_generated_data_uri_not_untrusted_url(self):
        self.provider.bridge.call_media.return_value = {'key': {'id': 'receipt'}}
        receipt = self.provider.deliver(instance='fixed', recipient='5511999999999@s.whatsapp.net',
                                        data=b'print(1)', mime_type='text/x-python', filename='file.py',
                                        caption='', kind='document')
        args = self.provider.bridge.call_media.call_args
        self.assertTrue(args.args[0].endswith('/message/document'))
        self.assertEqual(args.args[1]['fileName'], 'file.py')
        self.assertEqual(args.args[2], b'print(1)')
        self.assertEqual(args.args[3], 'text/x-python')
        self.assertEqual(receipt['message_id'], 'receipt')

    def test_decrypt_checks_ciphertext_mac_plaintext_and_length(self):
        encrypted, descriptor = encrypted_fixture()
        self.assertEqual(decrypt_image(encrypted, descriptor), PNG)
        for field, value in [('fileEncSha256', base64.b64encode(b'x' * 32).decode()),
                             ('fileSha256', base64.b64encode(b'x' * 32).decode()),
                             ('mediaKey', base64.b64encode(b'x' * 32).decode()), ('fileLength', '1')]:
            with self.subTest(field=field), self.assertRaises(FileError):
                decrypt_image(encrypted, dict(descriptor, **{field: value}))
        with self.assertRaises(FileError):
            decrypt_image(encrypted[:-1] + b'x', descriptor)

    def test_media_descriptor_cache_bound_to_chat_instance_and_expiry(self):
        encrypted, descriptor = encrypted_fixture()
        self.provider.clock = lambda: 100
        self.provider.remember_image('fixed', '5511999999999@s.whatsapp.net', 'known', descriptor, 1791774060)
        self.provider._download = Mock(return_value=encrypted)
        result = self.provider.read_image('fixed', '5511999999999@s.whatsapp.net', 'known')
        self.assertEqual(result['data'], PNG)
        for instance, chat in [('other', '5511999999999@s.whatsapp.net'), ('fixed', '5511888888888@s.whatsapp.net')]:
            with self.assertRaises(FileError):
                self.provider.read_image(instance, chat, 'known')
        self.provider.clock = lambda: 1000
        with self.assertRaises(FileError):
            self.provider.read_image('fixed', '5511999999999@s.whatsapp.net', 'known')

    def test_proxy_config_only_binds_loopback_and_uses_tls_hostname_verification(self):
        config = proxy_config()
        self.assertIn('listen 127.0.0.1:8092;', config)
        self.assertNotIn('listen 0.0.0.0', config)
        self.assertIn('proxy_ssl_verify on;', config)
        self.assertIn('proxy_pass_request_headers off;', config)
        self.assertIn('proxy_ssl_name files.oaiusercontent.com;', config)
        self.assertIn('proxy_ssl_name mmg.whatsapp.net;', config)
        self.assertIn('lylahub-mcp-fetch-auth.conf', config)

    def test_incomplete_download_does_not_forward_a_truncated_document(self):
        response = Response(b'partial')
        response.headers = {'Content-Length': '100'}
        self.provider.opener.open.return_value = response
        with patch('mcp_media.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            with self.assertRaises(FileError):
                self.provider.fetch_authorized(self.reference, 1024)

    def test_bridge_streams_base64_without_building_full_json_body(self):
        from server import Bridge
        import json
        payload = bytes(range(256)) * 1000
        with patch('server.http.client.HTTPConnection') as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"key":{"id":"receipt"}}'
            bridge = Bridge('fixture-secret', 'http://127.0.0.1:8080', 5)
            bridge.call_media('/v1/instance/fixed/message/document', {'number': 'fixture', 'caption': 'caption'},
                              payload, 'application/octet-stream')
            chunks = [call.args[0] for call in connection.send.call_args_list]
            self.assertLess(max(map(len, chunks)), 70000)
            body = b''.join(chunks)
            self.assertEqual(base64.b64decode(json.loads(body)['media'].split(',', 1)[1]), payload)
            headers = dict(call.args for call in connection.putheader.call_args_list)
            self.assertEqual(int(headers['Content-Length']), len(body))
            self.assertEqual(headers['apikey'], 'fixture-secret')
            connection.close.assert_called_once()

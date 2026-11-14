import io
import unittest
from unittest.mock import patch
from mcp_drive_client import DriveClient
from mcp_drive_transport import DriveTransport, DriveError
from mcp_archive_deploy import drive_proxy_config
from test_mcp_drive_oauth import Transport, response


class OAuth:
    def access_token(self): return 'fixture-access'


class DriveClientTests(unittest.TestCase):
    def test_query_deadline_applies_before_network_calls(self):
        transport = DriveTransport('x' * 48)
        with transport.scope(-1):
            with patch('mcp_drive_transport.socket.getaddrinfo') as dns:
                with self.assertRaisesRegex(DriveError, 'drive_timeout'):
                    transport.request('GET', '/api/drive/v3/files')
                dns.assert_not_called()

    def test_proxy_is_local_fixed_host_and_streamed(self):
        config = drive_proxy_config()
        self.assertIn('listen 127.0.0.1:8094;', config)
        self.assertIn('proxy_ssl_name oauth2.googleapis.com;', config)
        self.assertIn('proxy_ssl_name www.googleapis.com;', config)
        self.assertIn('proxy_request_buffering off;', config)
        self.assertIn('proxy_pass_request_headers off;', config)
        self.assertNotIn('resolver ', config)
        self.assertNotIn('192.0.2.20', config)

    def test_successful_upload_streams_same_id_and_size(self):
        transport = Transport(); drive = DriveClient(OAuth(), transport); drive.root_id = 'root-id'
        transport.responses += [response({'id': 'root-id', 'ownedByMe': True, 'shared': False,
                 'mimeType': 'application/vnd.google-apps.folder'}),
            response({}, headers={'location': 'https://www.googleapis.com/upload/drive/v3/files?upload_id=session'}),
            response({'id': 'new-file', 'ownedByMe': True, 'shared': False, 'size': '2'})]
        source = io.BytesIO(b'ab')
        result = drive.put('new-file', 'root-id', {'name': 'source.py'}, source, 2)
        self.assertEqual(result['id'], 'new-file')
        self.assertEqual(transport.calls[-1][2]['size'], 2)
        self.assertIs(transport.calls[-1][2]['body'], source)

    def test_id_and_root_creation_are_narrow(self):
        transport = Transport(); drive = DriveClient(OAuth(), transport)
        transport.responses += [response({'files': []}), response({'ids': ['generated-root']}),
            response({'id': 'generated-root', 'shared': False, 'ownedByMe': True,
                      'mimeType': 'application/vnd.google-apps.folder'})]
        self.assertEqual(drive.create_folder('LylaHub WhatsMiau', None, 'archive-root-v1'), 'generated-root')
        self.assertEqual(drive.root_id, 'generated-root')
        with self.assertRaises(DriveError): drive.create_folder('anywhere', None, 'another-root')

    def test_foreign_shared_and_trashed_files_are_denied(self):
        for extra in ({'parents': ['foreign-root']}, {'shared': True, 'parents': ['root-id']}, {'trashed': True}):
            transport = Transport(); drive = DriveClient(OAuth(), transport); drive.root_id = 'root-id'
            transport.responses.append(response(dict(id='file-id', ownedByMe=True, **extra)))
            transport.responses += [response({'id': 'foreign-root', 'parents': []}), response({'id': 'root-id'})]
            with self.assertRaises(DriveError): drive.read('file-id', 100)
            self.assertFalse(any('alt=media' in call[1] for call in transport.calls))

    def test_fixed_proxy_rejects_redirects_and_paths(self):
        transport = DriveTransport('x' * 48)
        for path in ('https://evil.test/file', '/api/../token', '/api/drive/v3/files/x\r\nX: y', '/token?x=y'):
            with self.assertRaises(DriveError): transport.request('GET', path)
        with self.assertRaises(DriveError): transport.request('GET', '/api/drive/v3/files', maximum=10485761)
        with patch('mcp_drive_transport.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(DriveError): transport.request('POST', '/token', body=b'')

    def test_generated_id_upload_rejects_untrusted_session_host(self):
        transport = Transport(); drive = DriveClient(OAuth(), transport); drive.root_id = 'root-id'
        transport.responses += [response({'id': 'root-id', 'ownedByMe': True, 'shared': False,
                                         'mimeType': 'application/vnd.google-apps.folder'}),
                               response({}, headers={'location': 'https://evil.test/upload-id'})]
        with self.assertRaises(DriveError): drive.put('file-id', 'root-id', {'name': 'safe.py'}, io.BytesIO(b'ab'), 2)
        self.assertEqual(len(transport.calls), 2)


if __name__ == '__main__': unittest.main()

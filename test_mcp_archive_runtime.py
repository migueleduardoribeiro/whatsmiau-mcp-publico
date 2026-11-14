import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from mcp_archive_runtime import ArchiveRuntime
from mcp_store import Store
from mcp_tools import Tools
from mcp_oauth import Principal
from test_mcp_archive_models import envelope
from test_mcp_archive_worker import FakeDrive


class ArchiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory / 'oauth.db'); self.addCleanup(self.store.close)
        self.config = {'instance_id': 'personal', 'allowed_user': 'owner', 'issuer': 'https://whatsmiau.example.org',
            'archive': {'directory': str(self.directory / 'archive'), 'credentials_path': str(self.directory / 'client.json'),
                'owner_email': 'owner@example.test', 'proxy_token': 'x' * 48, 'hook_token': 'y' * 48,
                'capture_enabled': True, 'trusted_view_once_metadata': True, 'relay_previous': False}}
        self.oauth = Mock(); self.oauth.health.return_value = {'requires_authorization': False}
        self.drive = FakeDrive()
        with patch('mcp_archive_runtime.DriveOAuth', return_value=self.oauth), patch('mcp_archive_runtime.DriveClient', return_value=self.drive):
            self.runtime = ArchiveRuntime(self.config, Mock(), self.store, Mock())
        self.addCleanup(self.runtime.stop)
        self.runtime.store.state('root_id', 'root')
    def test_authenticated_internal_capture(self):
        result = self.runtime.accept_webhook('personal', json.dumps(envelope()).encode())
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.runtime.store.health()['pending_events'], 1)
        with self.assertRaises(ValueError): self.runtime.accept_webhook('corporate', json.dumps(envelope()).encode())

    def test_group_events_enter_existing_private_archive_queue(self):
        event = envelope('group-message')
        event['data']['key']['remoteJid'] = '120363012345678901-123456@g.us'
        event['data']['key']['participant'] = '15557654321@s.whatsapp.net'
        event['data']['pushName'] = 'Ana'
        result = self.runtime.accept_webhook('personal', json.dumps(event).encode())
        self.assertEqual(result['status'], 'queued')
        queued = self.runtime.store.pending(1)[0]
        self.assertEqual(queued['chat'], '120363012345678901-123456@g.us')
        self.assertEqual(queued['participant_name'], 'Ana')
    def test_lifecycle_joins_worker(self):
        self.runtime.start(); self.assertTrue(self.runtime.thread.is_alive())
        self.runtime.stop(); self.assertFalse(self.runtime.thread.is_alive())
    def test_existing_webhook_forwarding_even_when_capture_fails(self):
        self.runtime.settings['relay_previous'] = True
        self.runtime.store.max_events = 1
        self.runtime._relay = Mock(return_value=None)
        self.runtime.accept_webhook('personal', json.dumps(envelope()).encode())
        self.runtime._relay.reset_mock()
        with self.assertRaises(Exception): self.runtime.accept_webhook('personal', json.dumps(envelope('second')).encode())
        self.runtime._relay.assert_called_once()
    def test_archive_failure_after_accepted_send_and_replay(self):
        self.runtime.store.max_events = 1
        self.runtime.accept_webhook('personal', json.dumps(envelope()).encode())
        bridge = Mock(); bridge.call.return_value = {'key': {'id': 'sent-id'}}
        tools = Tools(bridge, self.store, self.config, send_observer=self.runtime.record_accepted_send)
        principal = Principal('owner', 'personal', frozenset({'whatsapp.send'}))
        arguments = {'destinatario': '15551234567', 'texto': 'accepted send', 'operacao_id': 'one'}
        first = tools.call('enviar_texto', arguments, principal)
        second = tools.call('enviar_texto', arguments, principal)
        self.assertEqual(first['structuredContent']['status'], 'accepted')
        self.assertEqual(second['structuredContent'], first['structuredContent'])
        self.assertEqual(bridge.call.call_count, 1)
        self.assertTrue(self.runtime.health()['pending_capture'])

    def test_relay_survives_normalizer_rejection(self):
        self.runtime.settings['relay_previous'] = True
        self.runtime._relay = Mock()
        body = json.dumps(envelope(text='x' * 16385)).encode()
        with self.assertRaises(ValueError): self.runtime.accept_webhook('personal', body)
        self.runtime._relay.assert_called_once_with(body)
    def test_unverified_view_once_metadata_drops_media_caption(self):
        self.runtime.settings['trusted_view_once_metadata'] = False
        event = envelope(); event['data']['message'] = {'imageMessage': {'caption': 'private caption', 'fileLength': '10'}}
        self.runtime.accept_webhook('personal', json.dumps(event).encode())
        self.assertEqual(self.runtime.store.pending(1), [])

    def test_accepted_file_bytes_are_queued_and_uploaded_without_network_download(self):
        event = {'instance': 'personal', 'chat': envelope()['data']['key']['remoteJid'], 'message_id': 'file-send',
                 'text': 'caption', 'data': b'print(1)', 'media_kind': 'document', 'mime_type': 'text/x-python', 'filename': 'test.py'}
        self.assertEqual(self.runtime.record_accepted_send(event)['capture'], 'queued')
        self.assertEqual(len(list(self.runtime.spool.glob('accepted-*.bin'))), 1)
        self.runtime.worker.step()
        self.assertEqual(self.runtime.store.health()['pending_events'], 0)
        self.assertEqual(list(self.runtime.spool.iterdir()), [])
        self.assertIn(b'print(1)', self.drive.bytes.values())

    def test_accepted_group_file_send_is_queued_for_drive(self):
        event = {'instance': 'personal', 'chat': '120363012345678901-123456@g.us', 'message_id': 'group-file',
                 'text': 'caption', 'data': b'group-file', 'media_kind': 'document', 'mime_type': 'application/pdf',
                 'filename': 'group.pdf'}
        self.assertEqual(self.runtime.record_accepted_send(event)['capture'], 'queued')
        queued = self.runtime.store.pending(1)[0]
        self.assertEqual(queued['chat'], event['chat'])
        self.assertTrue(queued['from_me'])
        self.assertEqual(queued['media']['kind'], 'document')


if __name__ == '__main__': unittest.main()

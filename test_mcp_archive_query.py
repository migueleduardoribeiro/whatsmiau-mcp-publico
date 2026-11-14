import hashlib
import io
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from mcp_archive_models import normalize_event
from mcp_archive_store import ArchiveStore
from mcp_archive_worker import ArchiveWorker
from mcp_archive_query import ArchiveQuery
from mcp_store import Store
from mcp_oauth import Principal
from mcp_tools import Tools, instant
from mcp_files import FileGateway
from test_mcp_archive_models import envelope
from test_mcp_archive_worker import FakeDrive, NoMedia
from test_mcp_files import PNG


class ArchiveQueryTests(unittest.TestCase):
    def test_recent_window_prunes_earlier_messages_on_the_same_day(self):
        from datetime import datetime, timezone
        older = int(datetime(2026, 10, 26, 8, tzinfo=timezone.utc).timestamp())
        recent_time = int(datetime(2026, 10, 26, 19, 23, tzinfo=timezone.utc).timestamp())
        events = []
        for i in range(20):
            event = normalize_event(envelope('same-day-old-' + str(i)), 'personal')[0]
            event['timestamp'] = older
            events.append(event)
        recent = normalize_event(envelope('same-day-recent'), 'personal')[0]
        recent['timestamp'] = recent_time; events.append(recent)
        self.queue.accept(events); self.worker.step()
        _, manifest = self.worker.catalog.manifest('personal', self.chat)
        with patch.object(self.drive, 'metadata', wraps=self.drive.metadata) as metadata:
            page = self.messages(start=recent_time, end=recent_time + 1)
        self.assertEqual([m['id'] for m in page['mensagens']], ['same-day-recent'])
        self.assertLess(metadata.call_count, len(manifest['buckets']))

    def test_legacy_archive_backfill_prunes_unrelated_days(self):
        from unittest.mock import patch
        current = envelope()['data']['messageTimestamp']
        events = []
        for i in range(20):
            event = normalize_event(envelope('old-' + str(i)), 'personal')[0]
            event['timestamp'] = current - 864000
            events.append(event)
        recent = normalize_event(envelope('recent'), 'personal')[0]
        events.append(recent)
        self.queue.accept(events); self.worker.step()
        folder, manifest = self.worker.catalog.manifest('personal', self.chat)
        manifest.pop('time_index', None)
        self.worker.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        self.worker.schedule_index('personal', self.chat)
        for _ in range(500):
            if not self.worker.index_step(): break
        _, indexed = self.worker.catalog.manifest('personal', self.chat)
        self.assertTrue(indexed['time_index']['complete'])
        with patch.object(self.drive, 'metadata', wraps=self.drive.metadata) as metadata:
            page = self.messages(start=current, end=current + 1)
        self.assertEqual([m['id'] for m in page['mensagens']], ['recent'])
        self.assertFalse(page['cursor'])
        self.assertLess(metadata.call_count, len(manifest['buckets']))

    def test_received_only_query_returns_incoming_message(self):
        outgoing = envelope('sent')
        outgoing['data']['key']['fromMe'] = True
        incoming = envelope('reply')
        self.queue.accept(normalize_event(outgoing, 'personal') + normalize_event(incoming, 'personal'))
        self.worker.step()
        page = self.messages(limit=1, received_only=True)
        while not page['mensagens'] and page['cursor']:
            page = self.messages(limit=1, received_only=True, cursor=page['cursor'])
        self.assertEqual([m['id'] for m in page['mensagens']], ['reply'])

    def test_backfill_resumes_and_keeps_events_added_during_scan(self):
        current = envelope()['data']['messageTimestamp']
        self.seed(10)
        folder, manifest = self.worker.catalog.manifest('personal', self.chat)
        manifest.pop('time_index', None)
        self.worker.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        self.worker.schedule_index('personal', self.chat)
        self.assertTrue(self.worker.index_step())
        # An incomplete index must never suppress results.
        self.assertFalse(self.worker.catalog.manifest('personal', self.chat)[1]['time_index']['complete'])
        self.assertEqual(len(self.messages(start=current, end=current + 1)['mensagens']), 10)
        new = normalize_event(envelope('new-during-index'), 'personal')[0]
        new['timestamp'] = current + 86400
        self.queue.accept([new]); self.worker.step()
        restarted = ArchiveWorker(self.queue, self.drive, NoMedia(), transfer_slot=threading.BoundedSemaphore(1), clock=lambda: 1060)
        restarted.schedule_index('personal', self.chat)
        for _ in range(500):
            if not restarted.index_step(): break
        self.assertTrue(restarted.catalog.manifest('personal', self.chat)[1]['time_index']['complete'])
        self.assertEqual([m['id'] for m in self.messages(start=current + 86400, end=current + 86401)['mensagens']], ['new-during-index'])

    def test_mcp_exposes_received_only_filter_and_serial_guidance(self):
        from test_mcp_tools import FakeBridge
        outgoing = envelope('sent'); outgoing['data']['key']['fromMe'] = True
        self.queue.accept(normalize_event(outgoing, 'personal') + normalize_event(envelope('reply'), 'personal'))
        self.worker.step()
        tools = Tools(FakeBridge(), self.cursor_store, {'instance_id': 'personal'}, archive=self.query)
        principal = Principal('owner', 'personal', frozenset({'whatsapp.read'}))
        spec = next(item for item in tools.list_tools() if item['name'] == 'consultar_mensagens')
        self.assertIn('somente_recebidas', spec['inputSchema']['properties'])
        self.assertIn('sequencial', spec['description'].lower())
        output = tools.call('consultar_mensagens', {'destinatario': self.chat, 'inicio': '2026-10-16',
            'fim': '2026-10-18', 'somente_recebidas': True, 'limite': 100}, principal)
        self.assertFalse(output['isError'])
        self.assertEqual([m['id'] for m in output['structuredContent']['mensagens']], ['reply'])

    def test_stale_time_index_after_older_writer_falls_back_to_full_scan(self):
        self.seed()
        folder, manifest = self.worker.catalog.manifest('personal', self.chat)
        manifest['time_index']['slots'] = {}
        manifest['coverage']['last_committed_at'] += 1
        self.worker.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        self.assertEqual([m['id'] for m in self.messages()['mensagens']], ['m0'])

    def test_interrupted_catalog_write_never_trusts_obsolete_day_index(self):
        from mcp_archive_catalog import hash_id
        from mcp_drive_transport import DriveError
        self.seed()
        current = envelope()['data']['messageTimestamp']
        prefix = hash_id('m0')[:2]
        name = next('later-' + str(i) for i in range(10000) if hash_id('later-' + str(i))[:2] == prefix)
        later = normalize_event(envelope(name), 'personal')[0]
        later['timestamp'] = current + 86400
        original = self.drive.replace
        def fail_final(identifier, metadata, source, size):
            if metadata['name'] == 'current-v1.json':
                source.seek(0)
                import json
                value = json.load(source)
                if not value.get('time_index', {}).get('dirty'):
                    raise DriveError('fixture_interruption')
            return original(identifier, metadata, source, size)
        self.drive.replace = fail_final
        with self.assertRaises(DriveError):
            self.worker.catalog.update('personal', self.chat, [later], lambda _: self.drive.create_id(),
                                       {'last_committed_at': 2000})
        self.drive.replace = original
        page = self.messages(start=current + 86400, end=current + 86401)
        self.assertEqual([m['id'] for m in page['mensagens']], [name])

    def test_pending_batch_from_older_writer_disables_index_until_replayed(self):
        from mcp_archive_catalog import hash_id
        self.seed()
        current = envelope()['data']['messageTimestamp']
        prefix = hash_id('m0')[:2]
        name = next('old-writer-' + str(i) for i in range(10000) if hash_id('old-writer-' + str(i))[:2] == prefix)
        later = normalize_event(envelope(name), 'personal')[0]
        later['timestamp'] = current + 86400
        self.queue.accept([later])
        seq = self.queue.pending(100)[0]['_seq']
        self.queue.prepare('fixture', {'seqs': [seq]})
        folder, manifest = self.worker.catalog.manifest('personal', self.chat)
        self.worker.catalog._update_node(folder, prefix, manifest['buckets'][prefix], hash_id(name), later,
                                         lambda _: self.drive.create_id())
        query = ArchiveQuery(self.drive, cursor_store=self.cursor_store, health=self.queue.health)
        page = query.messages('owner', 'personal', {self.chat}, current + 86400, current + 86401, 50)
        self.assertEqual([m['id'] for m in page['mensagens']], [name])

    def test_group_archive_query_returns_sender_identity(self):
        event = envelope('group-message')
        group = '120363012345678901-123456@g.us'
        event['data']['key']['remoteJid'] = group
        event['data']['key']['participant'] = '15557654321@s.whatsapp.net'
        event['data']['pushName'] = 'Ana'
        self.queue.accept(normalize_event(event, 'personal'))
        self.worker.step()
        page = self.query.messages('owner', 'personal', {group}, 0, 2000000000, 50)
        message = page['mensagens'][0]
        self.assertEqual(message['participant'], '15557654321@s.whatsapp.net')
        self.assertEqual(message['participant_name'], 'Ana')

    def test_location_survives_archive_restart_and_is_projected_by_tool(self):
        event = envelope()
        event['data']['message'] = {'liveLocationMessage': {
            'degreesLatitude': -23.55, 'degreesLongitude': -46.63}}
        self.queue.accept(normalize_event(event, 'personal')); self.worker.step()
        query = ArchiveQuery(self.drive, cursor_store=self.cursor_store)
        from test_mcp_tools import FakeBridge
        tools = Tools(FakeBridge(), self.cursor_store, {'instance_id': 'personal'}, archive=query)
        principal = Principal('owner', 'personal', frozenset({'whatsapp.read'}))
        output = tools.call('consultar_mensagens', {'destinatario': self.chat,
            'inicio': '2026-10-17', 'fim': '2026-10-18'}, principal)
        self.assertFalse(output['isError'])
        location = output['structuredContent']['mensagens'][0]['location']
        self.assertEqual(location['latitude'], -23.55)
        self.assertEqual(location['longitude'], -46.63)
        self.assertEqual(location['position_status'], 'last_received')
        self.assertTrue(location['received_at'].endswith('+00:00'))
        deletion = {'instance': 'personal', 'event': 'messages.delete',
            'data': {'id': 'm1', 'remoteJid': self.chat, 'fromMe': False}}
        self.queue.accept(normalize_event(deletion, 'personal')); self.worker.step()
        self.assertEqual(self.messages()['mensagens'], [])

    def test_alias_manifests_and_nodes_are_read_once_per_query(self):
        self.seed(20)
        aliases={self.chat,'15559999999@lid'}
        with patch.object(self.query.catalog,'manifest',wraps=self.query.catalog.manifest) as manifests, patch.object(self.drive,'metadata',wraps=self.drive.metadata) as metadata:
            page=self.messages(chats=aliases,limit=100)
        self.assertEqual(len(page['mensagens']),20)
        self.assertLessEqual(manifests.call_count,2)
        ids=[call.args[0] for call in metadata.call_args_list]
        self.assertEqual(len(ids),len(set(ids)))
        # A later request must revalidate metadata and see deletions/edits.
        with patch.object(self.drive,'metadata',wraps=self.drive.metadata) as metadata:
            self.messages(chats=aliases,limit=100)
        self.assertGreater(metadata.call_count,0)

    def test_slow_read_returns_resumable_page_before_hard_timeout(self):
        self.seed(8)
        elapsed=[0]
        original=self.query._raw
        def read(*args,**kwargs):
            elapsed[0]+=16
            return original(*args,**kwargs)
        with patch('mcp_archive_query.time.monotonic',side_effect=lambda:elapsed[0]),patch.object(self.query,'_raw',side_effect=read):
            first=self.messages(limit=100)
        self.assertTrue(first['cursor'])
        self.assertLess(len(first['mensagens']),8)
        seen={m['id'] for m in first['mensagens']}; cursor=first['cursor']
        while cursor:
            page=self.messages(limit=100,cursor=cursor)
            current={m['id'] for m in page['mensagens']}
            self.assertFalse(seen & current); seen.update(current); cursor=page['cursor']
        self.assertEqual(len(seen),8)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.cursor_store = Store(directory / 'oauth.db'); self.addCleanup(self.cursor_store.close)
        self.queue = ArchiveStore(directory / 'queue.db', clock=lambda: 1000); self.addCleanup(self.queue.close)
        self.drive = FakeDrive(); self.queue.state('root_id', 'root')
        self.worker = ArchiveWorker(self.queue, self.drive, NoMedia(), transfer_slot=threading.BoundedSemaphore(1), clock=lambda: 1060)
        self.query = ArchiveQuery(self.drive, cursor_store=self.cursor_store)
        self.chat = envelope()['data']['key']['remoteJid']
    def seed(self, count=1):
        self.queue.accept([normalize_event(envelope('m' + str(i)), 'personal')[0] for i in range(count)])
        self.worker.step()
    def messages(self, **changes):
        args = dict(profile='owner', instance='personal', chats={self.chat}, start=0, end=2000000000, limit=50, cursor=None)
        args.update(changes); return self.query.messages(**args)
    def test_query_without_anchor(self):
        self.seed()
        class Bridge:
            def call(self, path, *args, **kwargs):
                if 'syncMessages' in path: raise AssertionError('unexpected phone history')
                return []
        tools = Tools(Bridge(), self.cursor_store, {'instance_id': 'personal'}, archive=self.query)
        principal = Principal('owner', 'personal', frozenset({'whatsapp.read'}))
        result = tools.call('consultar_mensagens', {'destinatario': self.chat, 'inicio': '2026-10-16', 'fim': '2026-10-17'}, principal)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['source'], 'drive_archive')
    def test_dates_cross_utc_day(self):
        self.seed()
        ts = envelope()['data']['messageTimestamp']
        self.assertEqual(self.messages(start=ts, end=ts + 1)['mensagens'][0]['id'], 'm0')
        self.assertEqual(self.messages(start=ts - 1, end=ts)['mensagens'], [])
        self.assertEqual(instant('2026-10-16'), instant('2026-10-16T03:00:00Z'))
    def test_cursor_scope_and_budget(self):
        self.seed(6)
        first = self.messages(limit=2)
        self.assertEqual(len(first['mensagens']), 2)
        self.assertTrue(first['cursor'])
        with self.assertRaises(ValueError): self.messages(limit=2, cursor=first['cursor'], profile='another')
        second = self.messages(limit=2, cursor=first['cursor'])
        self.assertFalse({m['id'] for m in first['mensagens']} & {m['id'] for m in second['mensagens']})
        self.assertLessEqual(self.query.last_scan['bytes'], 8388608)
        self.assertLessEqual(self.query.last_scan['batches'], 100)
        self.assertTrue(first['partial'])
    def test_late_edit_delete_projection(self):
        self.seed()
        edit = normalize_event(envelope('m0', text='new version'), 'personal')[0]
        self.queue.accept([edit]); self.worker.step()
        self.assertEqual(self.messages()['mensagens'][0]['text'], 'new version')
        deletion = normalize_event({'instance': 'personal', 'event': 'messages.delete',
            'data': {'id': 'm0', 'remoteJid': self.chat, 'fromMe': False}}, 'personal')[0]
        self.queue.accept([deletion]); self.worker.step()
        self.assertEqual(self.messages()['mensagens'], [])
    def test_empty_is_partial(self):
        result = self.messages()
        self.assertEqual(result['mensagens'], [])
        self.assertTrue(result['partial'])
        self.assertIn('capture_started_at', result['coverage'])

    def test_alias_tombstone_suppresses_phone_original(self):
        self.seed()
        lid = '15559999999@lid'
        self.queue.accept(normalize_event({'instance': 'personal', 'event': 'messages.delete',
            'data': {'id': 'm0', 'remoteJid': lid, 'fromMe': False}}, 'personal'))
        self.worker.step()
        self.assertEqual(self.messages(chats={self.chat, lid})['mensagens'], [])

    def test_output_size_paginates_escaped_text(self):
        import json
        from mcp_tools import result
        for i in range(100):
            self.queue.accept(normalize_event(envelope('large' + str(i), text='"' * 16384), 'personal'))
        while self.worker.step(): pass
        page = self.messages(limit=100)
        self.assertLess(len(json.dumps(result(page)).encode()), 4000000)
        self.assertTrue(page['cursor'])
    def test_image_from_archive_after_restart(self):
        self.seed()
        record = self.worker.catalog.current('personal', self.chat, 'm0')
        folder = self.worker.catalog.chat_folder('personal', self.chat)
        digest = hashlib.sha256(PNG).hexdigest()
        self.drive.put('image-id', folder, {'name': 'image.bin'}, io.BytesIO(PNG), len(PNG))
        record['media'] = {'kind': 'image', 'state': 'archived', 'drive_id': 'image-id', 'size': len(PNG),
                           'mime_type': 'image/png', 'sha256': digest}
        self.worker.catalog.update('personal', self.chat, [record], lambda key: self.drive.create_id(), {})
        again = ArchiveQuery(self.drive, cursor_store=self.cursor_store)
        image = again.image('owner', 'personal', self.chat, 'm0')
        self.assertEqual(image['data'], PNG)
        lid = '15559999999@lid'
        lid_record = dict(record, chat=lid)
        self.worker.catalog.update('personal', lid, [lid_record], lambda key: self.drive.create_id(), {})
        from unittest.mock import Mock
        bridge = Mock()
        bridge.call.return_value = [{'exists': True, 'jid': self.chat, 'number': self.chat.split('@')[0], 'lid': lid}]
        tools = Tools(bridge, self.cursor_store, {'instance_id': 'personal'}, archive=again,
                      file_gateway=FileGateway({'instance_id': 'personal'}, self.cursor_store, provider=Mock(), archive=again))
        # A PN request must find an image stored only under its verified LID.
        self.worker.catalog.update('personal', self.chat, [dict(record, message_id='other')], lambda key: self.drive.create_id(), {})
        lid_record = dict(record, chat=lid, message_id='lid-only')
        self.worker.catalog.update('personal', lid, [lid_record], lambda key: self.drive.create_id(), {})
        output_alias = tools.call('consultar_imagem', {'destinatario': self.chat, 'mensagem_id': 'lid-only'},
                                 Principal('owner', 'personal', frozenset({'whatsapp.read'})))
        self.assertFalse(output_alias['isError'])
        gateway = FileGateway({'instance_id': 'personal'}, self.cursor_store, archive=again)
        output = gateway.read_image(Principal('owner', 'personal', frozenset({'whatsapp.read'})), self.chat, 'm0')
        self.assertEqual(output['content'][-1]['mimeType'], 'image/png')
        self.assertNotIn('drive_id', str(self.messages()))

    def test_tool_describes_archive_without_anchor(self):
        tools = Tools(None, self.cursor_store, {'instance_id': 'personal'}, archive=self.query)
        description = next(item['description'] for item in tools.list_tools() if item['name'] == 'consultar_mensagens')
        self.assertIn('Drive', description)
        self.assertNotIn('reference_required', description)


if __name__ == '__main__': unittest.main()

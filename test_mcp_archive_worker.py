import hashlib
import io
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from mcp_archive_models import normalize_event
from mcp_archive_store import ArchiveStore
from mcp_archive_worker import ArchiveWorker
from mcp_drive_transport import DriveError
from test_mcp_archive_models import envelope


class FakeDrive:
    def __init__(self):
        self.root_id = 'root'
        self.files = {'root': {'id': 'root', 'name': 'LylaHub WhatsMiau', 'mimeType': 'application/vnd.google-apps.folder'}}
        self.bytes = {}; self.uploaded = []; self.next_id = 0; self.fail_batch = False
    def create_id(self): self.next_id += 1; return 'file-' + str(self.next_id)
    def create_folder(self, name, parent, identity):
        existing = self.lookup(parent, identity)
        if existing: return existing['id']
        identifier = self.create_id()
        self.files[identifier] = dict(id=identifier, name=name, parents=[parent] if parent else [],
            mimeType='application/vnd.google-apps.folder', appProperties={'archiveIdentity': identity})
        if parent is None: self.root_id = identifier
        return identifier
    def lookup(self, parent, identity):
        return next((v.copy() for v in self.files.values() if v.get('parents') == [parent]
                     and v.get('appProperties', {}).get('archiveIdentity') == identity), None)
    def metadata(self, identifier):
        if identifier not in self.files: raise DriveError('drive_api_failed', 404)
        return self.files[identifier].copy()
    def put(self, identifier, parent, metadata, source, size):
        if identifier in self.files: raise DriveError('drive_api_failed', 409)
        self.uploaded.append(identifier)
        result = self._save(identifier, parent, metadata, source, size)
        if self.fail_batch and metadata['name'].startswith('batch-'):
            self.fail_batch = False; raise DriveError('drive_timeout')
        return result
    def replace(self, identifier, metadata, source, size):
        return self._save(identifier, self.files[identifier]['parents'][0], metadata, source, size)
    def _save(self, identifier, parent, metadata, source, size):
        source.seek(0); value = source.read()
        if len(value) != size: raise AssertionError('incorrect upload length')
        self.bytes[identifier] = value
        self.files[identifier] = dict(metadata, id=identifier, parents=[parent], size=str(size),
                                     sha256Checksum=hashlib.sha256(value).hexdigest())
        return self.metadata(identifier)
    def read(self, identifier, maximum):
        data = self.bytes[identifier]
        if len(data) > maximum: raise DriveError('drive_response_limit')
        return data
    def list(self, parent, page_token=None):
        return {'files': [v.copy() for v in self.files.values() if v.get('parents') == [parent]]}


class NoMedia:
    @contextmanager
    def prepare(self, event): yield {'state': 'media_unavailable'}


class ArchiveWorkerTests(unittest.TestCase):
    def test_index_health_reports_only_aggregate_state(self):
        self.worker.schedule_index('personal', '15557654321@s.whatsapp.net')
        health = self.worker.index_health()
        self.assertEqual(health['index_jobs_pending'], 1)
        self.assertIsNone(health['index_error'])
        self.assertNotIn('15557654321', str(health))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.store = ArchiveStore(Path(self.temp.name) / 'queue.db', clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.drive = FakeDrive(); self.store.state('root_id', 'root')
        self.worker = ArchiveWorker(self.store, self.drive, NoMedia(), transfer_slot=threading.BoundedSemaphore(1), clock=lambda: self.now)
    def accept(self, identifier='m1', text='hello'):
        event = normalize_event(envelope(identifier, text=text), 'personal')[0]
        self.store.accept([event]); return event
    def test_batch_limits_and_flush_deadline(self):
        self.accept()
        self.assertFalse(self.worker.step())
        self.now += 60; self.assertTrue(self.worker.step())
        self.assertEqual(self.store.health()['pending_events'], 0)
        batches = [self.drive.bytes[k] for k, m in self.drive.files.items() if m['name'].startswith('batch-')]
        self.assertLessEqual(len(batches[0]), 1048576)
        self.assertEqual(len(batches[0].splitlines()), 1)
    def test_upload_ack_lost_reuses_id(self):
        self.accept(); self.now += 60; self.drive.fail_batch = True
        with self.assertRaises(DriveError): self.worker.step()
        identifier = self.store.prepared()[0]['drive_id']
        again = ArchiveWorker(self.store, self.drive, NoMedia(), transfer_slot=threading.BoundedSemaphore(1), clock=lambda: self.now)
        self.assertTrue(again.step())
        self.assertEqual(self.drive.uploaded.count(identifier), 1)
        self.assertEqual(self.store.health()['pending_events'], 0)
    def test_expired_media_keeps_text(self):
        event = self.accept(text='caption')
        event['message_id'] = 'media-1'; event['revision'] = 'media-revision'
        event['media'] = {'kind': 'image', 'url': 'https://mmg.whatsapp.net/expired', 'fileLength': '10'}
        self.store.accept([event]); self.now += 60
        self.worker.step()
        bodies = [v for k, v in self.drive.bytes.items() if self.drive.files[k]['name'].startswith('batch-')]
        self.assertIn(b'caption', b''.join(bodies))
        self.assertIn(b'media_unavailable', b''.join(bodies))
        self.assertNotIn(b'mmg.whatsapp.net', b''.join(bodies))
    def test_catalog_late_delete_and_original_message_cannot_resurrect(self):
        event = self.accept(); self.now += 60; self.worker.step()
        deletion = normalize_event({'instance': 'personal', 'event': 'messages.delete',
            'data': {'id': 'm1', 'remoteJid': event['chat'], 'fromMe': False}}, 'personal')[0]
        self.store.accept([deletion]); self.now += 60; self.worker.step()
        current = self.worker.catalog.current('personal', event['chat'], 'm1')
        self.assertEqual(current['kind'], 'delete')
        self.assertEqual(current['timestamp'], event['timestamp'])
    def test_hundred_events_flush_without_waiting(self):
        for i in range(100): self.accept(str(i))
        self.assertTrue(self.worker.step())
        self.assertEqual(self.store.health()['pending_events'], 0)

    def test_edit_before_original_remains_current(self):
        body = envelope(text='edited'); body['data']['isEdit'] = True
        edit = normalize_event(body, 'personal')[0]
        self.store.accept([edit]); self.now += 60; self.worker.step()
        original = normalize_event(envelope(text='original'), 'personal')[0]
        self.store.accept([original]); self.now += 60; self.worker.step()
        self.assertEqual(self.worker.catalog.current('personal', edit['chat'], 'm1')['text'], 'edited')

    def test_media_lost_ack_reconciles_before_expired_download(self):
        body = envelope(); body['data']['message'] = {'documentMessage': {'fileLength': '4'}}
        self.store.accept(normalize_event(body, 'personal'))
        class Media:
            calls = 0
            @contextmanager
            def prepare(self, event):
                self.calls += 1
                yield ({'state': 'archived', 'kind': 'document', 'source': io.BytesIO(b'data'),
                        'size': 4, 'sha256': hashlib.sha256(b'data').hexdigest()} if self.calls == 1
                       else {'state': 'media_unavailable'})
        media = Media(); self.worker.media = media
        original = self.drive.put
        def put(*args):
            result = original(*args)
            if args[2]['name'].startswith('attachment-'): raise DriveError('lost_ack')
            return result
        self.drive.put = put
        with self.assertRaises(DriveError): self.worker.step()
        self.drive.put = original
        self.assertTrue(self.worker.step())
        record = self.worker.catalog.current('personal', envelope()['data']['key']['remoteJid'], 'm1')
        self.assertEqual(record['media']['state'], 'archived')
        self.assertEqual(media.calls, 1)

    def test_failed_duplicate_media_does_not_replace_archived_media(self):
        event = self.accept(); self.now += 60; self.worker.step()
        current = self.worker.catalog.current('personal', event['chat'], 'm1')
        current['media'] = {'state': 'archived', 'drive_id': 'existing-image', 'kind': 'image'}
        counter = [0]
        def claim(identity): counter[0] += 1; return 'index-extra-' + str(counter[0])
        self.worker.catalog.update('personal', event['chat'], [current], claim, {})
        later = dict(current, order=current['order'] + 1, media={'state': 'media_unavailable'})
        self.worker.catalog.update('personal', event['chat'], [later], claim, {})
        result = self.worker.catalog.current('personal', event['chat'], 'm1')
        self.assertEqual(result['media']['drive_id'], 'existing-image')


if __name__ == '__main__': unittest.main()

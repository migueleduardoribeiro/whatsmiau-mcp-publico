import tempfile
import unittest
from pathlib import Path
from mcp_archive_models import normalize_event
from mcp_archive_store import ArchiveStore, ArchiveFull
from test_mcp_archive_models import envelope


class ArchiveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'queue.db'
        self.store = ArchiveStore(self.path, clock=lambda: 1000)
        self.addCleanup(lambda: self.store.close())

    def test_restart_retains_pending_and_capture_start(self):
        event = normalize_event(envelope(), 'personal')[0]
        self.assertEqual(self.store.accept([event, event]), 1)
        self.store.close()
        self.store = ArchiveStore(self.path, clock=lambda: 2000)
        self.assertEqual(self.store.pending(100)[0]['message_id'], 'm1')
        self.assertEqual(self.store.health()['capture_started_at'], 1000)

    def test_confirm_cleans_content_and_retains_bounded_dedupe(self):
        event = normalize_event(envelope(text='private_unique_body_3189'), 'personal')[0]
        self.store.accept([event]); row = self.store.pending(1)[0]
        self.store.prepare('batch1', {'seqs': [row['_seq']], 'drive_id': 'drive-file-id'})
        self.assertEqual(self.store.pending(100), [])
        self.store.close(); self.store = ArchiveStore(self.path)
        self.assertEqual(self.store.prepared()[0]['batch_id'], 'batch1')
        self.store.confirm('batch1')
        self.assertEqual(self.store.health()['pending_events'], 0)
        self.assertNotIn(b'private_unique_body_3189', self.path.read_bytes())
        self.assertEqual(self.store.accept([event]), 0)

    def test_event_limit_atomic_accept(self):
        self.store.close(); self.store = ArchiveStore(self.path, max_events=1)
        events = [normalize_event(envelope(str(i)), 'personal')[0] for i in range(2)]
        with self.assertRaises(ArchiveFull): self.store.accept(events)
        self.assertEqual(self.store.pending(10), [])

    def test_physical_queue_budget(self):
        self.store.close(); self.store = ArchiveStore(self.path, max_bytes=1048576)
        filled = False
        for i in range(30):
            event = normalize_event(envelope(str(i)), 'personal')[0]
            event['text'] = 'x' * 60000
            try: self.store.accept([event])
            except ArchiveFull: filled = True; break
        self.assertTrue(filled)
        self.assertLessEqual(sum(p.stat().st_size for p in self.path.parent.glob('queue.db*')), 1048576)
        self.assertTrue(self.store.health()['known_gaps'])

    def test_prepare_cannot_claim_foreign_or_already_claimed_rows(self):
        with self.assertRaises(ValueError): self.store.prepare('bad', {'seqs': [99]})
        self.store.accept(normalize_event(envelope(), 'personal'))
        seq = self.store.pending(1)[0]['_seq']
        self.store.prepare('first', {'seqs': [seq]})
        with self.assertRaises(ValueError): self.store.prepare('second', {'seqs': [seq]})


if __name__ == '__main__': unittest.main()

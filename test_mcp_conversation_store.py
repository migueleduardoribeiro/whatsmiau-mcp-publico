import tempfile
import unittest
from pathlib import Path
from mcp_conversation_store import ConversationStore


def snapshot(revision=2, *, complete=True):
    return dict(instance='personal', conversations=[dict(chat='5511999999999@s.whatsapp.net',
        aliases=['5511999999999@s.whatsapp.net'], name=None, kind='individual',
        last_message_at=100, unread_count=3, marked_unread=None,
        read_state=dict(value='unread', source='whatsapp', revision=str(revision), observed_at=100,
                        reason=None), unread_refs=None)], revision=str(revision), sequence=revision,
        observed_at=100, complete=complete, continuous=True, reason=None, cursor=None)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'conversations.db'
        self.store = ConversationStore(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.store.close())

    def test_chat_unread_does_not_identify_message_ids(self):
        self.store.apply(snapshot())
        item = self.store.snapshot('personal')['conversations'][0]
        self.assertEqual(item['unread_count'], 3)
        self.assertIsNone(item['unread_refs'])

    def test_manual_unread_is_not_message_unread(self):
        snap = snapshot()
        snap['conversations'][0].update(unread_count=None, marked_unread=True)
        self.store.apply(snap)
        item = self.store.snapshot('personal')['conversations'][0]
        self.assertIsNone(item['unread_count'])
        self.assertIsNone(item['unread_refs'])

    def test_old_snapshot_cannot_override_new_read(self):
        newest = snapshot(3)
        newest['conversations'][0].update(unread_count=0, unread_refs=[])
        newest['conversations'][0]['read_state']['value'] = 'read'
        self.store.apply(newest)
        self.store.apply(snapshot(2))
        self.assertEqual(self.store.snapshot('personal')['conversations'][0]['unread_count'], 0)

    def test_disconnect_gap_restart_require_reconciliation(self):
        self.store.apply(snapshot())
        self.store.invalidate('personal', 'disconnected')
        self.assertEqual(self.store.snapshot('personal')['conversations'][0]['read_state']['value'], 'unknown')
        self.store.apply(snapshot(3))
        self.store.close()
        self.store = ConversationStore(self.path)
        item = self.store.snapshot('personal')['conversations'][0]
        self.assertEqual(item['read_state']['value'], 'unknown')
        self.assertIsNone(item['unread_count'])

    def test_full_sync_missing_events_is_unknown(self):
        snap = snapshot()
        snap['continuous'] = False
        self.store.apply(snap)
        self.assertEqual(self.store.snapshot('personal')['conversations'][0]['read_state']['value'], 'unknown')

    def test_own_receipt_is_not_recipient_receipt(self):
        self.store.apply(snapshot())
        self.store.invalidate('personal', 'receipt_only')
        self.assertIsNone(self.store.snapshot('personal')['conversations'][0]['unread_refs'])

    def test_same_operation_cannot_change_set_or_profile(self):
        self.assertIsNone(self.store.claim('owner', 'personal', 'page-1', {'ids': ['a']}))
        self.store.finish('owner', 'personal', 'page-1', {'status': 'submitted'})
        self.assertEqual(self.store.claim('owner', 'personal', 'page-1', {'ids': ['a']})['status'], 'submitted')
        with self.assertRaisesRegex(ValueError, 'operation_conflict'):
            self.store.claim('owner', 'personal', 'page-1', {'ids': ['b']})
        self.assertIsNone(self.store.claim('other', 'personal', 'page-1', {'ids': ['b']}))

    def test_snapshot_rejects_bodies_and_inconsistent_refs(self):
        snap = snapshot()
        snap['conversations'][0]['text'] = 'private body'
        with self.assertRaisesRegex(ValueError, 'invalid_arguments'):
            self.store.apply(snap)


if __name__ == '__main__': unittest.main()

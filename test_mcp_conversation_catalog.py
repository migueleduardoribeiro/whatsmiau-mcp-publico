import hashlib
import tempfile
import unittest
from pathlib import Path
from mcp_archive_catalog import ArchiveCatalog
from mcp_archive_models import normalize_event
from mcp_archive_query import ArchiveQuery
from mcp_conversation_catalog import ConversationCatalog
from mcp_conversation_source import ConversationSource
from mcp_store import Store
from test_mcp_archive_models import envelope
from test_mcp_archive_worker import FakeDrive
from test_mcp_conversation_source import Bridge


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tickets.db'); self.addCleanup(self.store.close)
        self.drive = FakeDrive(); self.archive = ArchiveQuery(self.drive, cursor_store=self.store)
        self.archive._begin()
        self.bridge = Bridge(); self.source = ConversationSource(self.bridge, 'owner', 'personal',receipts_enabled=True)
        self.catalog = ConversationCatalog(self.archive, self.source)

    def add(self, chat, identifier='a'):
        event = normalize_event(envelope(identifier), 'personal')[0]
        event.update(chat=chat, order=1)
        ArchiveCatalog(self.drive).update('personal', chat, [event], lambda _: self.drive.create_id(),
                                         {'last_committed_at': 100})
        self.archive.cache.clear(); self.archive._begin()

    def test_existing_hash_named_folders_without_directory(self):
        chat = '5511999999999@s.whatsapp.net'; self.add(chat)
        folder, manifest = self.archive.catalog.manifest('personal', chat)
        manifest.pop('conversation', None)
        self.archive.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        page = self.catalog.list('owner', 'personal')
        self.assertEqual([row['chat'] for row in page['conversations']], [chat])
        self.assertEqual(page['conversations'][0]['read_state']['value'], 'unknown')
        self.assertFalse(page['complete'])
        self.assertTrue(page['archive_complete'])
        self.assertFalse(any(call[1] != 'GET' for call in self.bridge.calls))

    def test_unsaved_individual_and_group_are_discovered(self):
        self.add('5511888888888@s.whatsapp.net')
        self.add('120363000000000001@g.us')
        rows = self.catalog.list('owner', 'personal')['conversations']
        self.assertEqual({row['kind'] for row in rows}, {'individual', 'group'})
        self.assertNotIn('text', str(rows))

    def test_partial_source_never_claims_empty_inbox(self):
        self.add('5511999999999@s.whatsapp.net')
        page = self.catalog.list('owner', 'personal', unread_only=True)
        self.assertEqual(page['conversations'], [])
        self.assertEqual(len(page['unknown_conversations']), 1)
        self.assertFalse(page['complete'])
        self.assertFalse(any(call[1] != 'GET' for call in self.bridge.calls))

    def test_phone_state_uses_known_anchor_without_exposing_or_marking_content(self):
        from unittest.mock import patch
        chat='5511999999999@s.whatsapp.net'; self.add(chat)
        self.source.phone_state=True
        with patch.object(self.bridge,'call',return_value=dict(chat=chat,unreadCount=2,markedAsUnread=False)):
            page=self.catalog.list('owner','personal',unread_only=True)
        self.assertEqual(page['conversations'][0]['unread_count'],2)
        self.assertEqual(page['conversations'][0]['read_state']['value'],'unread')
        self.assertIsNone(page['conversations'][0]['unread_refs'])
        self.assertNotIn('_anchor',str(page))
        self.assertNotIn('hello',str(page))

    def test_cursor_binds_filters_profile_and_returns_all_pages(self):
        self.add('5511999999999@s.whatsapp.net'); self.add('5511888888888@s.whatsapp.net')
        first = self.catalog.list('owner', 'personal', limit=1)
        self.assertIsNotNone(first['cursor'])
        with self.assertRaisesRegex(ValueError, 'invalid_cursor'):
            self.catalog.list('owner', 'personal', limit=2, cursor=first['cursor'])
        # Wrong filter must not consume a legitimate cursor.
        second = self.catalog.list('owner', 'personal', limit=1, cursor=first['cursor'])
        self.assertEqual(len(first['conversations'] + second['conversations']), 2)
        self.assertIsNone(second['cursor'])

    def test_wrong_folder_hash_is_not_a_conversation(self):
        chat='5511999999999@s.whatsapp.net'; self.add(chat)
        folder, _ = self.archive.catalog.manifest('personal', chat)
        self.drive.files[folder]['appProperties']['archiveIdentity'] = 'chat:' + 'a'*64
        page = self.catalog.list('owner', 'personal')
        self.assertEqual(page['conversations'], [])
        self.assertTrue(page['coverage']['known_gaps'])


if __name__ == '__main__': unittest.main()

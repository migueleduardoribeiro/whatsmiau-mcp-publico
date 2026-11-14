import hashlib
import unittest
from unittest.mock import patch
from mcp_conversation_query import ConversationQuery
from mcp_archive_query import BudgetExceeded
from test_mcp_conversation_catalog import CatalogTests


class QueryTests(CatalogTests):
    def setUp(self):
        super().setUp()
        self.query = ConversationQuery(self.archive, self.catalog)
        self.chat = '5511999999999@s.whatsapp.net'

    def test_global_top_k_latest_is_in_last_hash_bucket(self):
        ids = sorted(['a', 'b', 'c', 'd', 'e'], key=lambda x: hashlib.sha256(x.encode()).hexdigest())
        for i, identifier in enumerate(ids):
            self.add(self.chat, identifier)
        # Choose records by their real fixture timestamp; force final hash bucket newest.
        for identifier, timestamp in zip(ids, [101, 102, 103, 104, 999]):
            folder, manifest = self.archive.catalog.manifest('personal', self.chat)
            digest = hashlib.sha256(identifier.encode()).hexdigest()
            node_id = manifest['buckets'][digest[:2]]
            node = self.archive.catalog.read_node(node_id)
            node['records'][digest]['timestamp'] = timestamp
            import io, json
            raw = json.dumps(node).encode()
            self.drive.replace(node_id, {'name': 'leaf', 'appProperties': {}}, io.BytesIO(raw), len(raw))
        self.archive.cache.clear()
        self.archive._begin()
        folder, manifest = self.archive.catalog.manifest('personal', self.chat)
        manifest.pop('time_index', None)
        self.archive.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        self.archive.cache.clear()
        selection = self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=2000, limit=2)
        self.assertTrue(selection['selection_complete'])
        self.assertEqual([(r['id'], r['timestamp']) for r in selection['refs']], [(ids[-1], 999), (ids[-2], 104)])

    def test_budget_pending_has_no_false_latest(self):
        self.add(self.chat)
        original = self.archive._raw
        with patch.object(self.archive, '_raw', side_effect=BudgetExceeded):
            page = self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=4133980799, limit=1)
        self.assertEqual(page['refs'], [])
        self.assertFalse(page['selection_complete'])
        self.assertIsNotNone(page['cursor'])
        final = self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=4133980799,
                                  limit=1, cursor=page['cursor'])
        self.assertEqual([r['id'] for r in final['refs']], ['a'])

    def test_two_chats_interleave_and_equal_timestamps_are_stable(self):
        other='120363000000000001@g.us'
        self.add(self.chat, 'same'); self.add(other, 'same')
        result = self.query.recent('owner', 'personal', chats=[other, self.chat], start=1, end=4133980799)
        self.assertEqual(len(result['refs']), 2)
        self.assertEqual([r['chat'] for r in result['refs']], sorted([other, self.chat], reverse=True))

    def test_known_count_unknown_ids_does_not_guess_last_n(self):
        self.add(self.chat)
        selection = self.query.unread('owner', 'personal', chats=[self.chat])
        self.assertEqual(selection['refs'], [])
        self.assertEqual(selection['read_state']['value'], 'unknown')

    def test_degraded_archive_has_no_recent_cutoff_and_cursor_cannot_change_purpose(self):
        from mcp_archive_catalog import ArchiveCatalog
        from mcp_archive_models import normalize_event
        from test_mcp_archive_models import envelope
        record=normalize_event(envelope('ancient'),'personal')[0]
        record.update(chat=self.chat,timestamp=100,order=1)
        ArchiveCatalog(self.drive).update('personal',self.chat,[record],lambda _:self.drive.create_id(),{})
        self.add(self.chat,'newer')
        self.query.unread_fallback=True
        first=self.query.unread('owner','personal',chats=[self.chat],limit=1)
        binding=self.store.peek_ticket(first['cursor'],'conversation_recent','owner')['binding']
        with self.assertRaisesRegex(ValueError,'invalid_cursor'):
            self.query.recent('owner','personal',chats=[self.chat],start=1,end=binding['end'],limit=1,cursor=first['cursor'])
        final=self.query.unread('owner','personal',chats=[self.chat],limit=1,cursor=first['cursor'])
        self.assertEqual(final['refs'][0]['id'],'ancient')
        self.assertEqual(final['read_state']['value'],'unknown')

    def test_verified_aliases_merge_discovery_and_respect_cross_alias_edit_delete(self):
        from mcp_archive_models import normalize_event
        from mcp_archive_catalog import ArchiveCatalog
        from test_mcp_archive_models import envelope
        lid='992222222222222@lid'
        self.add(self.chat,'edited'); self.add(self.chat,'deleted'); self.add(lid,'only-lid')
        self.add('120363000000000001@g.us','deleted')
        edit=normalize_event(envelope('edited',text='updated'), 'personal')[0]
        edit.update(chat=lid,is_edit=True,order=2)
        deletion=normalize_event({'instance':'personal','event':'messages.delete',
            'data':{'id':'deleted','remoteJid':lid,'fromMe':False}},'personal')[0]
        deletion['order']=3
        ArchiveCatalog(self.drive).update('personal',lid,[edit,deletion],lambda _:self.drive.create_id(),{})
        self.archive.cache.clear(); self.archive._begin()
        identity=dict(chat=self.chat,aliases=[self.chat,lid],resolution_complete=True)
        original=self.source.identity
        def resolve(profile,instance,chat,**kwargs):
            return identity if chat in identity['aliases'] else original(profile,instance,chat,**kwargs)
        with patch.object(self.source,'identity',side_effect=resolve):
            rows=self.catalog.list('owner','personal')['conversations']
            self.assertEqual(len(rows),2)
            first=self.query.recent('owner','personal',chats=[self.chat],start=1,end=4133980799,limit=1)
            second=self.query.recent('owner','personal',chats=[self.chat],start=1,end=4133980799,limit=1,cursor=first['cursor'])
            refs=first['refs']+second['refs']
            self.assertEqual({ref['id'] for ref in refs},{'edited','only-lid'})
            self.assertTrue(all(ref['chat']==lid for ref in refs))
            global_page=self.query.recent('owner','personal',start=1,end=4133980799)
            self.assertEqual(len(global_page['refs']),3)

    def test_completed_pages_continue_without_skipping_timestamp_ties(self):
        for identifier in ('one','two','three'): self.add(self.chat,identifier)
        pages=[]; cursor=None
        for _ in range(3):
            page=self.query.recent('owner','personal',chats=[self.chat],start=1,end=4133980799,limit=1,cursor=cursor)
            pages.extend(ref['id'] for ref in page['refs'])
            cursor=page['cursor']
            if len(pages)<3: self.assertIsNotNone(cursor)
        self.assertEqual(pages,['two','three','one'])
        self.assertIsNone(cursor)

    def test_resolve_changed_revision_never_returns_stale_content(self):
        self.add(self.chat)
        selection = self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=4133980799)
        ref = dict(selection['refs'][0], revision='wrong')
        with self.assertRaisesRegex(ValueError, 'archive_changed_retry'):
            self.archive.resolve_refs('owner', 'personal', [ref])

    def test_revision_change_invalidates_selection(self):
        self.add(self.chat)
        original = self.archive._time_buckets
        changed = False
        def concurrent_write(manifest, start, end):
            nonlocal changed
            if not changed:
                changed = True
                from mcp_archive_catalog import ArchiveCatalog
                from mcp_archive_models import normalize_event
                from test_mcp_archive_models import envelope
                event = normalize_event(envelope('new-arrival'), 'personal')[0]
                event.update(chat=self.chat, order=2)
                ArchiveCatalog(self.drive).update('personal', self.chat, [event],
                    lambda _: self.drive.create_id(), {'last_committed_at': 101})
            return original(manifest, start, end)
        with patch.object(self.archive, '_time_buckets', side_effect=concurrent_write):
            with self.assertRaisesRegex(ValueError, 'archive_changed_retry'):
                self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=4133980799)


if __name__ == '__main__': unittest.main()

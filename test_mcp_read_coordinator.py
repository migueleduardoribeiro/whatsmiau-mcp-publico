import unittest
from mcp_conversation_query import ConversationQuery
from mcp_conversation_store import ConversationStore
from mcp_read_coordinator import ReadCoordinator
from test_mcp_conversation_catalog import CatalogTests
from pathlib import Path


class PresentationTests(CatalogTests):
    def setUp(self):
        super().setUp()
        self.state = ConversationStore(Path(self.tmp.name) / 'read.db'); self.addCleanup(self.state.close)
        self.coordinator = ReadCoordinator(self.archive, self.source, self.state)
        self.query = ConversationQuery(self.archive, self.catalog)
        self.chat='5511999999999@s.whatsapp.net'

    def selection(self):
        return self.query.recent('owner', 'personal', chats=[self.chat], start=1, end=4133980799, limit=1)

    def test_only_returned_received_ids_are_marked(self):
        self.add(self.chat, 'one'); self.add(self.chat, 'two')
        selected = self.selection()
        result = self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual([m['id'] for m in result['mensagens']], ['two'])
        self.assertEqual([call[2]['readMessages'][0]['id'] for call in self.bridge.calls if call[0].endswith('/read-messages')], ['two'])
        self.assertEqual(result['read_result']['status'], 'uncertain')

    def photo_selection(self):
        from mcp_archive_models import normalize_event
        from mcp_archive_catalog import ArchiveCatalog
        from test_mcp_archive_models import envelope
        event=envelope('photo')
        event['data']['message']={'imageMessage':{'mimetype':'image/jpeg',
            'url':'https://mmg.whatsapp.net/synthetic','fileLength':'100'}}
        record=normalize_event(event,'personal')[0];record.update(chat=self.chat,order=1)
        record['media']['state']='archived'
        ArchiveCatalog(self.drive).update('personal',self.chat,[record],lambda _:self.drive.create_id(),{})
        self.archive.cache.clear();self.archive._begin()
        return self.selection()

    def test_image_metadata_without_opening_content_sends_no_receipt(self):
        selection=self.photo_selection()
        result=self.coordinator.present('owner','personal',selection,'photo-metadata')
        self.assertTrue(result['mensagens'][0]['has_image'])
        self.assertEqual(result['mensagens'][0]['text'],'')
        self.assertEqual(result['read_result']['status'],'not_applicable')
        self.assertEqual(result['read_result']['items'][0]['reason'],'metadata_only')
        self.assertFalse(any(call[0].endswith('/read-messages') for call in self.bridge.calls))

    def test_opened_image_content_marks_only_its_message(self):
        selection=self.photo_selection();selection['content_opened']=True
        result=self.coordinator.present('owner','personal',selection,'photo-opened')
        self.assertNotIn('content_opened',result)
        self.assertEqual(result['read_result']['status'],'uncertain')
        calls=[call for call in self.bridge.calls if call[0].endswith('/read-messages')]
        self.assertEqual([item['id'] for item in calls[0][2]['readMessages']],['photo'])

    def test_retry_same_operation_never_adds_ids(self):
        self.add(self.chat,'one')
        selected=self.selection()
        first=self.coordinator.present('owner','personal',selected,'page-1')
        second=self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual(first,second)
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)
        self.add(self.chat,'two')
        with self.assertRaisesRegex(ValueError,'operation_conflict'):
            self.coordinator.present('owner','personal',self.selection(),'page-1')
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)

    def test_serialization_failure_marks_nothing(self):
        self.add(self.chat)
        selection=self.selection(); selection['coverage']['bad']=float('nan')
        with self.assertRaises(ValueError):
            self.coordinator.present('owner','personal',selection,'page-1')
        self.assertFalse(any(call[1]!='GET' for call in self.bridge.calls))

    def test_unshown_pages_and_new_arrivals_remain_unread(self):
        self.add(self.chat,'one'); selected=self.selection(); self.add(self.chat,'two')
        result=self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual([m['id'] for m in result['mensagens']],['one'])
        self.assertEqual([call for call in self.bridge.calls if call[0].endswith('/read-messages')][0][2]['readMessages'][0]['id'],'one')

    def test_pending_scan_and_empty_summary_mark_nothing(self):
        self.add(self.chat)
        selected=self.selection(); selected.update(refs=[],selection_complete=False,cursor='pending')
        result=self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual(result['read_result']['status'],'not_applicable')
        self.assertFalse(any(call[1]!='GET' for call in self.bridge.calls))

    def test_replay_rehydrates_refs_without_resending_receipts(self):
        self.add(self.chat)
        selected=self.selection()
        self.coordinator.present('owner','personal',selected,'page-1',request={'filter':'recent'})
        replay=self.coordinator.replay('owner','personal','page-1',{'filter':'recent'})
        self.assertEqual(replay['mensagens'][0]['id'],'a')
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)
        with self.assertRaisesRegex(ValueError,'operation_conflict'):
            self.coordinator.replay('owner','personal','page-1',{'filter':'other'})

    def test_result_write_failure_returns_content_and_does_not_resend(self):
        from unittest.mock import patch
        import sqlite3
        self.add(self.chat)
        selected=self.selection()
        with patch.object(self.state,'finish',side_effect=sqlite3.OperationalError('disk full')):
            result=self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual(result['mensagens'][0]['id'],'a')
        self.assertEqual(result['read_result']['status'],'uncertain')
        repeated=self.coordinator.present('owner','personal',selected,'page-1')
        self.assertEqual(repeated['read_result']['operation_id'],'page-1')
        self.assertIn('items',repeated['read_result'])
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith('/read-messages')]),1)


if __name__=='__main__': unittest.main()

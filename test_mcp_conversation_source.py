import unittest
from mcp_conversation_source import ConversationSource


class Bridge:
    def __init__(self): self.calls = []
    def call(self, path, method='GET', body=None, timeout=None):
        self.calls.append((path, method, body))
        return {'status': 'success'}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.source = ConversationSource(self.bridge, 'owner', 'personal',receipts_enabled=True)

    def test_receipts_default_disabled_until_selective_behavior_is_verified(self):
        source=ConversationSource(self.bridge,'owner','personal')
        ref=dict(instance='personal',chat='5511999999999@s.whatsapp.net',id='one',
            from_me=False,participant=None,timestamp=100,revision='v1')
        result=source.mark_read('owner','personal',[ref],'read-disabled')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'selective_read_not_verified')
        self.assertEqual(self.bridge.calls,[])

    def test_optional_unread_is_unknown(self):
        snap = self.source.snapshot('owner', 'personal')
        self.assertFalse(snap['complete'])
        self.assertEqual(snap['reason'], 'whatsapp_read_state_unverified')
        self.assertEqual(self.bridge.calls, [])

    def test_identity_requires_verified_pair_and_reuses_reverse_mapping(self):
        from unittest.mock import patch
        pn='5511999999999@s.whatsapp.net'; lid='992222222222222@lid'
        with patch.object(self.bridge,'call',return_value=dict(requested_chat=pn,
                aliases=[pn,lid],canonical_chat=pn,resolution_complete=True)) as call:
            value=self.source.identity('owner','personal',pn)
            self.assertEqual(value['aliases'],sorted([pn,lid]))
            self.assertEqual(self.source.identity('owner','personal',lid),value)
            self.assertEqual(call.call_count,1)
        self.source.identities.clear()
        with patch.object(self.bridge,'call',return_value=dict(requested_chat=pn,
                aliases=[pn,'5511888888888@s.whatsapp.net'],canonical_chat=pn,resolution_complete=True)):
            self.assertEqual(self.source.identity('owner','personal',pn)['aliases'],[pn])

    def test_phone_count_and_manual_marker_do_not_invent_unread_ids(self):
        from unittest.mock import patch
        self.source.phone_state=True
        anchor=dict(id='known',timestamp=1792245600,from_me=False)
        for raw,expected in [(dict(unreadCount=2,markedAsUnread=False),'unread'),
                (dict(unreadCount=0,markedAsUnread=False),'read'),
                (dict(unreadCount=0,markedAsUnread=True),'unread'),
                (dict(unreadCount=None,markedAsUnread=False),'unknown'),
                (dict(unreadCount=0,markedAsUnread=None),'unknown')]:
            with patch.object(self.bridge,'call',return_value=dict(raw,chat='5511999999999@s.whatsapp.net')):
                value=self.source.state('owner','personal','5511999999999@s.whatsapp.net',anchor)
            self.assertEqual(value['read_state']['value'],expected)
            self.assertEqual(value['unread_count'],raw['unreadCount'])
            self.assertIsNone(value['unread_refs'])

    def test_wrong_profile_or_instance_never_calls_upstream(self):
        for profile, instance in [('other', 'personal'), ('owner', 'other')]:
            with self.assertRaisesRegex(ValueError, 'access_denied'):
                self.source.snapshot(profile, instance)
        self.assertEqual(self.bridge.calls, [])

    def test_http_success_with_swallowed_receipt_error_is_uncertain(self):
        ref = dict(instance='personal', chat='120363000000000001@g.us', id='one',
                   from_me=False, participant='5511999999999@s.whatsapp.net', timestamp=100, revision='v1')
        result = self.source.mark_read('owner', 'personal', [ref], 'page-1')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.bridge.calls, [('/v1/instance/personal/chat/read-messages', 'POST',
            {'readMessages': [{'remoteJid': '120363000000000001@g.us', 'id': 'one',
                              'sender': '5511999999999@s.whatsapp.net', 'played': False}]})])

    def test_group_missing_sender_fails_without_marking_chat(self):
        ref = dict(instance='personal', chat='120363000000000001@g.us', id='one',
                   from_me=False, participant=None, timestamp=100, revision='v1')
        result = self.source.mark_read('owner', 'personal', [ref], 'page-1')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.bridge.calls, [])

    def test_validation_precedes_all_effects(self):
        refs = [dict(instance='other', chat='5511999999999@s.whatsapp.net', id='x',
                     from_me=False, participant=None, timestamp=100, revision='v1')]
        with self.assertRaisesRegex(ValueError, 'invalid_arguments'):
            self.source.mark_read('owner', 'personal', refs, 'page-1')
        self.assertEqual(self.bridge.calls, [])

    def test_group_receipts_batch_only_same_chat_and_sender(self):
        ref = dict(instance='personal', chat='120363000000000001@g.us', id='one',
                   from_me=False, participant='5511999999999@s.whatsapp.net', timestamp=100, revision='v1')
        refs=[ref, dict(ref,id='two'), dict(ref,id='three',participant='5511888888888@s.whatsapp.net')]
        self.source.mark_read('owner','personal',refs,'page-1')
        self.assertEqual(len(self.bridge.calls),2)
        self.assertEqual([r['id'] for r in self.bridge.calls[0][2]['readMessages']],['one','two'])
        self.assertEqual(self.bridge.calls[1][2]['readMessages'][0]['sender'],'5511888888888@s.whatsapp.net')
        self.assertTrue(all(item['played'] is False for call in self.bridge.calls for item in call[2]['readMessages']))


if __name__ == '__main__': unittest.main()

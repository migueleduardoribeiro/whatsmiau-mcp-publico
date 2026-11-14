import copy
import unittest
from mcp_archive_models import normalize_event


def envelope(identifier='m1', *, text='hello', own=False):
    return {'instance': 'personal', 'event': 'messages.upsert',
            'data': {'key': {'id': identifier, 'remoteJid': '15551234567@s.whatsapp.net', **({'fromMe': True} if own else {})},
                     'messageTimestamp': 1792245600, 'message': {'conversation': text}}}


class ArchiveModelsTests(unittest.TestCase):
    def test_locations_preserve_coordinates_and_drop_untrusted_extras(self):
        event = envelope()
        event['data']['message'] = {'locationMessage': {
            'degreesLatitude': -23.55, 'degreesLongitude': -46.63,
            'name': 'Praça', 'address': 'Rua de teste',
            'url': 'https://untrusted.invalid/private', 'jpegThumbnail': 'private'}}
        location = normalize_event(event, 'personal')[0]['location']
        self.assertEqual(location, {'type': 'static', 'latitude': -23.55, 'longitude': -46.63,
            'name': 'Praça', 'address': 'Rua de teste',
            'map_url': 'https://www.google.com/maps/search/?api=1&query=-23.55%2C-46.63'})

    def test_live_location_is_a_received_position_and_changes_revision(self):
        event = envelope()
        event['data']['message'] = {'liveLocationMessage': {
            'degreesLatitude': -23.55, 'degreesLongitude': -46.63, 'caption': 'Aqui',
            'accuracyInMeters': 25, 'jpegThumbnail': 'private'}}
        first = normalize_event(event, 'personal')[0]
        self.assertEqual(first['location']['type'], 'live')
        self.assertEqual(first['location']['position_status'], 'last_received')
        self.assertEqual(first['location']['accuracy_meters'], 25)
        self.assertEqual(first['location']['caption'], 'Aqui')
        event['data']['message']['liveLocationMessage']['degreesLatitude'] = -23.56
        self.assertNotEqual(first['revision'], normalize_event(event, 'personal')[0]['revision'])

    def test_invalid_location_does_not_lose_other_message_content(self):
        for latitude, longitude in [(True, 0), ('1', 0), (91, 0), (0, -181), (float('nan'), 0),
                                    (0, float('inf')), (None, 0), (10 ** 1000, 0)]:
            with self.subTest(latitude=latitude, longitude=longitude):
                event = envelope()
                event['data']['message']['locationMessage'] = {
                    'degreesLatitude': latitude, 'degreesLongitude': longitude}
                record = normalize_event(event, 'personal')[0]
                self.assertIsNone(record.get('location'))
                self.assertEqual(record['text'], 'hello')

    def test_zero_coordinates_omitted_by_upstream_are_preserved(self):
        event = envelope()
        event['data']['message'] = {'locationMessage': {'degreesLongitude': 0.1}}
        self.assertEqual(normalize_event(event, 'personal')[0]['location']['latitude'], 0)

    def test_top_level_view_once_wrapper_flag_excludes_content(self):
        event = envelope(text='private once-only content')
        event['data']['isViewOnce'] = True
        self.assertEqual(normalize_event(event, 'personal'), [])

    def test_direction_and_view_once(self):
        self.assertFalse(normalize_event(envelope(), 'personal')[0]['from_me'])
        self.assertTrue(normalize_event(envelope(own=True), 'personal')[0]['from_me'])
        event = envelope()
        event['data']['message'] = {'imageMessage': {'url': 'https://mmg.whatsapp.net/a', 'viewOnce': True}}
        self.assertEqual(normalize_event(event, 'personal'), [])
        event['data']['message'] = {'viewOnceMessageV2': {'message': {'conversation': 'private'}}}
        self.assertEqual(normalize_event(event, 'personal'), [])

    def test_identity_and_non_individuals(self):
        with self.assertRaises(ValueError): normalize_event(envelope(), 'other')
        for jid in ('status@broadcast', '1234@g.us', 'malformed'):
            event = envelope(); event['data']['key']['remoteJid'] = jid
            self.assertEqual(normalize_event(event, 'personal'), [])
        event = envelope(); event['data']['key']['fromMe'] = 'false'
        with self.assertRaises(ValueError): normalize_event(event, 'personal')

    def test_group_audio_preserves_chat_and_participant_for_archive(self):
        event = envelope('group-audio')
        event['data']['key']['remoteJid'] = '120363012345678901-123456@g.us'
        event['data']['key']['participant'] = '15557654321@s.whatsapp.net'
        event['data']['pushName'] = 'Ana'
        event['data']['message'] = {'audioMessage': {
            'url': 'https://mmg.whatsapp.net/audio', 'mimetype': 'audio/ogg; codecs=opus',
            'fileLength': '123'}}
        records = normalize_event(event, 'personal')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['chat'], '120363012345678901-123456@g.us')
        self.assertEqual(records[0]['participant'], '15557654321@s.whatsapp.net')
        self.assertEqual(records[0]['participant_name'], 'Ana')
        self.assertEqual(records[0]['media']['kind'], 'audio')

    def test_history_and_receipts_are_distinct(self):
        original = envelope()
        history = {'instance': 'personal', 'event': 'messages.set', 'data': [original['data']]}
        self.assertEqual(normalize_event(history, 'personal')[0]['source'], 'history_event')
        receipt = {'instance': 'personal', 'event': 'messages.update', 'data': {'messageId': 'm1', 'status': 'READ'}}
        self.assertEqual(normalize_event(receipt, 'personal'), [])

    def test_revision_dedupe_and_late_delete(self):
        event = envelope()
        first = normalize_event(event, 'personal')[0]
        later = copy.deepcopy(event); later['date_time'] = '2026-10-18T01:00:00Z'
        self.assertEqual(first['revision'], normalize_event(later, 'personal')[0]['revision'])
        later['data']['message']['conversation'] = 'edited'
        self.assertNotEqual(first['revision'], normalize_event(later, 'personal')[0]['revision'])
        deleted = normalize_event({'instance': 'personal', 'event': 'messages.delete',
                                  'data': {'id': 'm1', 'remoteJid': first['chat'], 'fromMe': False}}, 'personal')[0]
        self.assertEqual(deleted['kind'], 'delete')
        self.assertIsNone(deleted['timestamp'])

    def test_media_allowlist_drops_api_keys_and_quoted_bodies(self):
        event = envelope(); event['apikey'] = 'must-not-save'
        event['data']['message'] = {'documentMessage': {'fileName': 'source.py', 'fileLength': '4',
                  'url': 'https://mmg.whatsapp.net/a', 'mediaKey': 'descriptor', 'contextInfo': {'quotedMessage': 'private'}}}
        item = normalize_event(event, 'personal')[0]
        self.assertEqual(item['media']['kind'], 'document')
        self.assertNotIn('contextInfo', item['media'])
        self.assertNotIn('must-not-save', str(item))


if __name__ == '__main__': unittest.main()

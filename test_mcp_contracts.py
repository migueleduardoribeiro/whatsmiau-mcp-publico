import unittest
from mcp_contracts import instance_path, capabilities_from_contract, inspect_contracts


class ContractsTests(unittest.TestCase):
    def test_instance_id_is_url_encoded(self):
        self.assertEqual(instance_path('A B/ç', 'contact'), '/v1/instance/A%20B%2F%C3%A7/contact')

    def test_group_listing_uses_allowlisted_instance_route(self):
        self.assertEqual(instance_path('fixture', 'group/fetchAllGroups'),
                         '/v1/instance/fixture/group/fetchAllGroups')

    def test_sync_requires_anchor(self):
        result = capabilities_from_contract({'paths': {'/v1/chat/syncMessages/{instance}': {}}})
        self.assertTrue(result['history_requires_reference'])
        self.assertFalse(result['history_bootstrap'])

    def test_missing_media_route_is_not_download_support(self):
        result = capabilities_from_contract({'paths': {'/v1/message/sendMedia/{instance}': {}}})
        self.assertFalse(result['image_download'])

    def test_contacts_shape_and_no_data_leak(self):
        class Bridge:
            def call(self, path):
                if path.endswith('swagger.json'):
                    return {'paths': {}}
                if path.endswith('/contact'):
                    return [{'jid': 'sensitive', 'fullName': 'private'}]
                return {'instance': {'state': 'open'}}
        result = inspect_contracts(Bridge(), 'fixture')
        self.assertTrue(result['contacts'])
        self.assertEqual(result['contact_count'], 1)
        self.assertNotIn('sensitive', str(result))
        self.assertNotIn('private', str(result))

    def test_unknown_operation_is_rejected(self):
        with self.assertRaises(ValueError):
            instance_path('id', '../logout')


class HistoryTransportTests(unittest.TestCase):
    def test_http504_is_preserved_without_upstream_body(self):
        import io
        import urllib.error
        from unittest.mock import Mock
        from server import APIError, Bridge
        bridge = Bridge('fixture', 'http://127.0.0.1:8080', 1)
        bridge.opener = Mock()
        bridge.opener.open.side_effect = urllib.error.HTTPError('http://fixture', 504, 'timeout', {},
                                                               io.BytesIO(b'private-body'))
        with self.assertRaises(APIError) as raised:
            bridge.call('/v1/instance/fixed/chat/syncMessages', 'POST', {})
        self.assertEqual(raised.exception.status, 504)
        self.assertNotIn('private-body', raised.exception.message)

"""Management tests: every write uses a mocked WhatsMiau, never real instances."""
import json
import io
import unittest
from unittest.mock import Mock
from server import Bridge, APIError
import test_server
from test_server import SECRET, ORIGIN


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge(SECRET, 'http://127.0.0.1:8080', 1)
        self.current = {'url': 'https://hooks.example.org/incoming', 'enabled': True,
                        'base64': False, 'events': ['MESSAGES_UPSERT'],
                        'headers': {'Authorization': 'hidden-header'}}
        self.writes = []
        def call(path, method='GET', body=None, **kwargs):
            if path == '/v1/instance/fetchInstances':
                return [{'id': 'sample', 'instanceName': 'Atendimento', 'syncFullHistory': False}]
            if path == '/v1/webhook/find/sample':
                return {'webhook': dict(self.current)}
            if path == '/v1/webhook/set/sample':
                self.writes.append((path, method, body))
                self.current.update(body['webhook'])
                return {'webhook': dict(self.current)}
            self.writes.append((path, method, body))
            return {'message': SECRET}
        self.bridge.call = Mock(side_effect=call)

    def payload(self):
        result = self.bridge.webhook('sample')
        return {**result['webhook'], 'revision': result['revision']}

    def test_read_filters_headers_and_lists_only_emitted_events(self):
        result = self.bridge.webhook('sample')
        self.assertNotIn('hidden-header', json.dumps(result))
        self.assertFalse(result['capabilities']['headers'])
        self.assertTrue(result['capabilities']['base64'])
        self.assertEqual(result['capabilities']['scope'], 'instance')
        self.assertNotIn('GROUPS_UPSERT', result['supported_events'])
        self.assertIn('CALL', result['supported_events'])

    def test_legacy_url_secrets_are_not_revealed_in_initial_form(self):
        for url in ('https://user:password@example.org', 'https://example.org/hook?token=private',
                    'https://example.org/' + SECRET):
            self.current['url'] = url
            result = self.bridge.webhook('sample')
            self.assertEqual(result['webhook']['url'], '')
            self.assertTrue(result['sensitive_url'])
            self.assertNotIn('password', json.dumps(result))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_save_reads_back_and_preserves_unused_headers(self):
        payload = self.payload()
        payload.update(url='https://hooks.example.org/new', base64=True, events=['CALL'])
        result = self.bridge.set_webhook('sample', payload)
        self.assertEqual(result['webhook']['events'], ['CALL'])
        self.assertTrue(result['webhook']['base64'])
        self.assertEqual(self.current['headers'], {'Authorization': 'hidden-header'})
        self.assertNotIn('headers', self.writes[0][2]['webhook'])
        self.assertEqual(self.writes[0][:2], ('/v1/webhook/set/sample', 'POST'))

    def test_conflict_does_not_overwrite(self):
        payload = self.payload()
        self.current['events'] = ['CALL']
        with self.assertRaises(APIError) as error:
            self.bridge.set_webhook('sample', payload)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.writes, [])

    def test_validation_before_write(self):
        for update in ({'url': 'http://127.0.0.1/private'}, {'url': 'https://user:secret@example.org'},
                       {'url': 'https://example.org/?token=secret'}, {'url': 'https://192.0.2.20/x'},
                       {'url': 'https://localhost/x'}, {'events': ['FAKE']}, {'events': []},
                       {'enabled': 'false'}, {'base64': 1}, {'headers': {'X': 'secret'}},
                       {'url': ''}, {'revision': 'stale'}):
            with self.subTest(update=update), self.assertRaises(APIError):
                self.bridge.set_webhook('sample', {**self.payload(), **update})
        self.assertEqual(self.writes, [])

    def test_unconfigured_defaults_and_empty_disabled_events(self):
        self.current = {}
        result = self.bridge.webhook('sample')
        self.assertEqual(result['webhook'], {'url': '', 'enabled': True, 'base64': False, 'events': []})
        payload = self.payload()
        payload.update(url='https://hooks.example.org/new', enabled=False)
        self.assertEqual(self.bridge.set_webhook('sample', payload)['webhook']['events'], [])

    def test_gateway_forwarding_cannot_be_bypassed(self):
        self.current['url'] = 'http://192.0.2.10:8081/webhooks/sample'
        self.assertTrue(self.bridge.webhook('sample')['managed'])
        for update in ({'url': 'https://example.org'}, {'enabled': False}, {'base64': True}, {'events': ['CALL']}):
            with self.subTest(update=update), self.assertRaises(APIError):
                self.bridge.set_webhook('sample', {**self.payload(), **update})
        self.assertEqual(self.writes, [])
        self.bridge.set_webhook('sample', {**self.payload(), 'events': ['MESSAGES_UPSERT', 'CALL']})

    def test_logout_delete_require_confirmation_and_current_name(self):
        for action in ('logout', 'delete'):
            for payload in ({}, {'confirm': False}, {'confirm': True, 'name': 'Wrong'}):
                with self.subTest(action=action, payload=payload), self.assertRaises(APIError):
                    self.bridge.remove('sample', action, payload)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.bridge.remove('sample', 'logout', {'confirm': True, 'name': 'Atendimento'}), {'ok': True})
        self.assertEqual(self.writes[-1][:2], ('/v1/instance/logout/sample', 'DELETE'))
        self.bridge.remove('sample', 'delete', {'confirm': True, 'name': 'Atendimento'})
        self.assertEqual(self.writes[-1][:2], ('/v1/instance/delete/sample', 'DELETE'))
        self.bridge.call.side_effect = APIError(502, 'safe error')
        with self.assertRaises(APIError):
            self.bridge.remove('sample', 'delete', {'confirm': True, 'name': 'Atendimento'})

    def test_details_no_individual_token_or_global_key(self):
        details = self.bridge.details('sample', ORIGIN)
        self.assertEqual(details['credential_scope'], 'global')
        self.assertEqual(details['api_url'], ORIGIN + '/v1')
        self.assertNotIn(SECRET, json.dumps(details))
        with self.assertRaises(APIError):
            self.bridge.details('missing', ORIGIN)

    def test_installed_closed_state_allows_normal_reconnection_display(self):
        self.bridge.call = Mock(return_value={'instance': {'state': 'closed'}})
        self.assertEqual(self.bridge.state('sample'), 'close')


class ManagementHTTPTests(test_server.PanelTests):
    def test_http_webhook_save_errors_and_destructive_confirmations_mocked(self):
        path = '/instances/first-uuid/webhook'
        status, found, _ = self.request(path)
        self.assertEqual(status, 200)
        payload = {**found['webhook'], 'url': 'https://hooks.example.org/incoming',
                   'enabled': True, 'events': ['CONNECTION_UPDATE'], 'revision': found['revision']}
        status, saved, _ = self.request(path, 'POST', payload)
        self.assertEqual(status, 200)
        self.assertEqual(saved['webhook']['url'], payload['url'])
        self.assertEqual(self.request(path, 'POST', payload)[0], 409)
        self.upstream.failure = True
        status, error, _ = self.request(path, 'POST', {**payload, 'revision': saved['revision']})
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET, json.dumps(error))
        self.upstream.failure = False
        for action in ('logout', 'delete'):
            self.assertEqual(self.request('/instances/first-uuid/' + action, 'POST', {'confirm': True, 'name': 'wrong'})[0], 400)
        name = self.upstream.instances[0]['instanceName']
        self.assertEqual(self.request('/instances/first-uuid/logout', 'POST', {'confirm': True, 'name': name})[0], 200)
        self.assertEqual(len(self.upstream.instances), 1)
        self.assertEqual(self.request('/instances/first-uuid/connect', 'POST', {})[0], 200)
        self.assertEqual(self.request('/instances/first-uuid/delete', 'POST', {'confirm': True, 'name': name})[0], 200)
        self.assertEqual(self.upstream.instances, [])

    def test_reveal_requires_authenticated_user_and_csrf(self):
        path = '/credentials/reveal'
        for method, headers in [('GET', {'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'usuario-exemplo'}),
                                ('POST', {'X-LylaHub-Panel': '1', 'Origin': ORIGIN}),
                                ('POST', {'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'usuario-exemplo', 'Origin': 'https://evil.invalid'}),
                                ('POST', {'X-LylaHub-User': 'usuario-exemplo', 'Origin': ORIGIN})]:
            status, body, _ = self.request(path, method, {}, headers)
            self.assertIn(status, (403, 404))
            self.assertNotIn(SECRET, json.dumps(body))
        status, body, headers = self.request(path, 'POST', {'confirm': True})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'api_key': SECRET, 'scope': 'global'})
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(self.request(path, 'POST', {})[0], 400)

    def test_management_paths_reject_cross_origin_and_arbitrary_methods(self):
        for suffix in ('webhook', 'logout', 'delete'):
            path = '/instances/first-uuid/' + suffix
            status, _, _ = self.request(path, 'POST', {}, {'Origin': 'https://evil.invalid', 'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'usuario-exemplo'})
            self.assertEqual(status, 403)
            self.assertEqual(self.request(path, 'DELETE', {})[0], 404)
        self.assertEqual(self.request('/instances', headers={'X-LylaHub-Panel': '1'})[0], 403)


class HTTPRejectionTests(unittest.TestCase):
    def test_rejected_small_body_is_consumed_before_socket_closes(self):
        from server import Handler
        handler = object.__new__(Handler)
        handler.headers = {'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}')
        handler.reply = Mock()
        handler.dispatch()
        self.assertEqual(handler.rfile.tell(), 2)
        self.assertEqual(handler.reply.call_args.args[0], 403)


if __name__ == '__main__':
    unittest.main()

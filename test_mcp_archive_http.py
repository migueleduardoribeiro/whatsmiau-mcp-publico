import json
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode
from mcp_archive_http import ArchiveHTTP


class ArchiveHTTPTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.config = {'issuer': 'https://whatsmiau.example.org', 'allowed_user': 'owner'}
        self.runtime.settings = {'owner_email': 'owner@example.test', 'hook_token': 'x' * 48}
        self.runtime.health.return_value = {'pending_events': 0}
        self.runtime.oauth.begin.return_value = 'https://accounts.google.com/o/oauth2/v2/auth?state=fixture'
        self.runtime.mcp_store.ticket.return_value = 'csrf-ticket'
        self.http = ArchiveHTTP(self.runtime)
        self.login = {'X-LylaHub-Archive-Login': '1', 'X-LylaHub-User': 'owner', 'Origin': 'https://whatsmiau.example.org'}
    def test_internal_requires_independent_marker_and_token(self):
        status, _, _ = self.http.handle('POST', '/archive/events/personal', {}, b'{}')
        self.assertEqual(status, 403)
        self.runtime.accept_webhook.assert_not_called()
        headers = {'X-LylaHub-Archive-Hook': '1', 'X-LylaHub-Archive-Token': 'x' * 48, 'Content-Type': 'application/json'}
        self.assertEqual(self.http.handle('POST', '/archive/events/personal', headers, b'{}')[0], 202)
    def test_start_status_require_login_and_origin(self):
        self.assertEqual(self.http.handle('GET', '/drive/status', {}, b'')[0], 403)
        self.assertEqual(self.http.handle('GET', '/drive/status', self.login, b'')[0], 200)
        self.assertEqual(self.http.handle('POST', '/drive/oauth/start', dict(self.login, Origin='https://evil.test'), b'csrf=x')[0], 403)
        self.assertEqual(self.http.handle('GET', '/drive/oauth/start', self.login, b'')[0], 200)
        headers = dict(self.login, **{'Content-Type': 'application/x-www-form-urlencoded'})
        status, response_headers, _ = self.http.handle('POST', '/drive/oauth/start', headers, b'csrf=csrf-ticket')
        self.assertEqual(status, 303)
        self.assertTrue(response_headers['Location'].startswith('https://accounts.google.com/'))
    def test_callback_strict_parameters_and_fixed_result(self):
        self.assertEqual(self.http.handle('GET', '/drive/oauth/callback?state=a&state=b&code=x', {}, b'')[0], 400)
        self.runtime.oauth.complete.assert_not_called()
        status, headers, body = self.http.handle('GET', '/drive/oauth/callback?state=fixture&code=sensitive-fixture-code', {}, b'')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/drive/status')
        self.assertNotIn('sensitive-fixture-code', str(body))

    def test_http_header_names_are_case_insensitive(self):
        login = {key.lower(): value for key, value in self.login.items()}
        self.assertEqual(self.http.handle('GET', '/drive/status', login, b'')[0], 200)

    def test_google_callback_issuer_is_validated(self):
        path = '/drive/oauth/callback?state=fixture&code=fixture&iss=https%3A%2F%2Faccounts.google.com'
        self.assertEqual(self.http.handle('GET', path, {}, b'')[0], 303)
        self.runtime.oauth.complete.reset_mock()
        self.assertEqual(self.http.handle('GET', path.replace('accounts.google.com', 'evil.test'), {}, b'')[0], 400)
        self.runtime.oauth.complete.assert_not_called()

    def test_authenticated_navigation_from_external_site_is_allowed_but_post_is_not(self):
        login = dict(self.login, **{'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(self.http.handle('GET', '/drive/oauth/start', login, b'')[0], 200)
        self.assertEqual(self.http.handle('GET', '/drive/status', login, b'')[0], 200)
        self.assertEqual(self.http.handle('POST', '/drive/oauth/start', login, b'csrf=x')[0], 403)


if __name__ == '__main__': unittest.main()

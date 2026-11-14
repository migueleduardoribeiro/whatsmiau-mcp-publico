import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from mcp_drive_oauth import DriveOAuth, DriveError, SCOPE, CALLBACK
from mcp_drive_transport import Response


class Transport:
    def __init__(self): self.calls = []; self.responses = []
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.responses.pop(0)


def response(data, status=200, headers=None): return Response(status, headers or {}, json.dumps(data).encode())


class DriveOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.credentials, self.state, self.tokens = [directory / name for name in ('client.json', 'state.json', 'token.json')]
        self.credentials.write_text(json.dumps({'web': {'client_id': 'fixture.apps.googleusercontent.com',
            'client_secret': 'fixture-secret', 'redirect_uris': [CALLBACK]}}))
        self.transport = Transport(); self.now = 1000
        self.oauth = DriveOAuth(self.credentials, self.state, self.tokens, self.transport, clock=lambda: self.now)

    def grant_responses(self, **overrides):
        self.transport.responses += [response(dict(access_token='access', refresh_token='refresh', expires_in=3600,
                                                   scope=SCOPE, token_type='Bearer', **overrides)),
                                     response({'user': {'emailAddress': 'owner@example.test'}})]

    def begin(self):
        return {k: v[0] for k, v in parse_qs(urlsplit(self.oauth.begin('owner@example.test')).query).items()}

    def test_state_single_use_and_pkce(self):
        params = self.begin(); self.grant_responses()
        self.assertEqual(params['scope'], SCOPE)
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertEqual(params['redirect_uri'], CALLBACK)
        self.oauth.complete({'state': params['state'], 'code': 'a-code'})
        sent = parse_qs(self.transport.calls[0][2]['body'].decode())
        actual = base64.urlsafe_b64encode(hashlib.sha256(sent['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'], actual)
        self.assertEqual(self.oauth.access_token(), 'access')
        with self.assertRaises(DriveError): self.oauth.complete({'state': params['state'], 'code': 'a-code'})

    def test_expired_and_invalid_state_never_contacts_google(self):
        params = self.begin(); self.now += 601
        with self.assertRaises(DriveError): self.oauth.complete({'state': params['state'], 'code': 'x'})
        with self.assertRaises(DriveError): self.oauth.complete({'state': 'invalid', 'code': 'x'})
        self.assertEqual(self.transport.calls, [])

    def test_refresh_preserves_refresh_token(self):
        params = self.begin(); self.grant_responses(); self.oauth.complete({'state': params['state'], 'code': 'x'})
        self.now += 3601
        self.transport.responses.append(response({'access_token': 'new-access', 'expires_in': 3600, 'token_type': 'Bearer'}))
        self.assertEqual(self.oauth.access_token(), 'new-access')
        self.assertEqual(json.loads(self.tokens.read_text())['refresh_token'], 'refresh')

    def test_insufficient_scope_and_wrong_owner(self):
        params = self.begin()
        self.transport.responses.append(response({'access_token': 'access', 'refresh_token': 'refresh',
            'expires_in': 3600, 'scope': 'https://www.googleapis.com/auth/drive', 'token_type': 'Bearer'}))
        with self.assertRaises(DriveError): self.oauth.complete({'state': params['state'], 'code': 'x'})
        self.assertFalse(self.tokens.exists())
        params = self.begin(); self.transport.responses += [response({'access_token': 'a', 'refresh_token': 'r',
            'scope': SCOPE, 'token_type': 'Bearer', 'expires_in': 3600}), response({'user': {'emailAddress': 'wrong@example.test'}})]
        with self.assertRaises(DriveError): self.oauth.complete({'state': params['state'], 'code': 'x'})
        self.assertFalse(self.tokens.exists())

    def test_invalid_grant_requires_reauthorization_and_preserves_file(self):
        params = self.begin(); self.grant_responses(); self.oauth.complete({'state': params['state'], 'code': 'x'})
        self.now += 3601
        self.transport.responses.append(response({'error': 'invalid_grant', 'error_description': 'secret upstream detail'}, 400))
        with self.assertRaisesRegex(DriveError, '^drive_authorization_required$'): self.oauth.access_token()
        self.assertTrue(self.oauth.health()['requires_authorization'])
        reopened = DriveOAuth(self.credentials, self.state, self.tokens, self.transport, clock=lambda: self.now)
        self.assertTrue(reopened.health()['requires_authorization'])
        self.assertNotIn('refresh', str(self.oauth.health()))


if __name__ == '__main__': unittest.main()

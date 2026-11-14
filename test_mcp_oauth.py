import base64
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from mcp_store import Store, StateError
from mcp_oauth import OAuth, AuthError


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.now = 1000
        self.store = Store(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.oauth = OAuth(self.store, 'https://fixture.example', 'https://fixture.example/mcp',
                           {'client': {'redirect_uris': ['https://chatgpt.com/connector/oauth/callback']}},
                           'fixed-instance', allowed_user='owner')
        self.verifier = 'v' * 43
        self.params = {'client_id': 'client', 'redirect_uri': 'https://chatgpt.com/connector/oauth/callback',
                       'response_type': 'code', 'resource': 'https://fixture.example/mcp',
                       'scope': 'whatsapp.read whatsapp.send', 'state': 'state-value',
                       'code_challenge_method': 'S256', 'code_challenge':
                       base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')}

    def code(self):
        return self.oauth.authorize('owner', self.params)['code']

    def exchange(self, code, **changes):
        params = dict(grant_type='authorization_code', code=code, client_id='client',
                      redirect_uri=self.params['redirect_uri'], code_verifier=self.verifier,
                      resource=self.params['resource'])
        params.update(changes)
        return self.oauth.exchange(params)

    def test_pkce_and_instance_scope(self):
        tokens = self.exchange(self.code())
        principal = self.oauth.authenticate(tokens['access_token'], 'whatsapp.send')
        self.assertEqual(principal.instance_id, 'fixed-instance')
        self.assertEqual(tokens['expires_in'], 3600)

    def test_bad_pkce_and_reused_code(self):
        code = self.code()
        with self.assertRaises(AuthError):
            self.exchange(code, code_verifier='x' * 43)
        code = self.code()
        self.exchange(code)
        with self.assertRaises(AuthError):
            self.exchange(code)

    def test_code_expiration(self):
        code = self.code()
        self.now += 60
        with self.assertRaises(AuthError):
            self.exchange(code)

    def test_redirect_resource_user_and_scope_are_checked(self):
        for field, value in [('redirect_uri', 'https://evil.example'), ('resource', 'other'),
                             ('scope', 'admin'), ('code_challenge_method', 'plain')]:
            with self.subTest(field=field), self.assertRaises(AuthError):
                self.oauth.authorize('owner', dict(self.params, **{field: value}))
        with self.assertRaises(AuthError):
            self.oauth.authorize('other-user', self.params)
        with self.assertRaises(AuthError):
            self.exchange(self.code(), resource='other')

    def test_read_grant_cannot_send(self):
        self.params['scope'] = 'whatsapp.read'
        token = self.exchange(self.code())['access_token']
        with self.assertRaises(AuthError):
            self.oauth.authenticate(token, 'whatsapp.send')

    def test_access_and_refresh_expiration(self):
        tokens = self.exchange(self.code())
        self.now += 3600
        with self.assertRaises(AuthError):
            self.oauth.authenticate(tokens['access_token'], 'whatsapp.read')
        self.now += 30 * 86400
        with self.assertRaises(AuthError):
            self.oauth.exchange(dict(grant_type='refresh_token', refresh_token=tokens['refresh_token'],
                                     client_id='client', resource=self.params['resource']))

    def test_refresh_rotation_replay_revokes_family_after_restart(self):
        tokens = self.exchange(self.code())
        params = dict(grant_type='refresh_token', refresh_token=tokens['refresh_token'],
                      client_id='client', resource=self.params['resource'])
        fresh = self.oauth.exchange(params)
        with self.assertRaises(AuthError):
            self.oauth.exchange(params)
        self.store.close()
        self.store = Store(self.path, clock=lambda: self.now)
        self.oauth.store = self.store
        with self.assertRaises(AuthError):
            self.oauth.authenticate(fresh['access_token'], 'whatsapp.read')

    def test_csrf_consent_is_user_bound_and_single_use(self):
        ticket = self.oauth.prepare_consent('owner', self.params)
        with self.assertRaises(AuthError):
            self.oauth.consent('other-user', ticket)
        result = self.oauth.consent('owner', ticket)
        self.assertEqual(result['state'], self.params['state'])
        with self.assertRaises(AuthError):
            self.oauth.consent('owner', ticket)

    def test_tokens_are_hashed_on_disk(self):
        tokens = self.exchange(self.code())
        self.assertNotIn(tokens['access_token'].encode(), self.path.read_bytes())
        self.assertNotIn(tokens['refresh_token'].encode(), self.path.read_bytes())

    def test_same_send_key_concurrently_claimed_once(self):
        barrier = threading.Barrier(2)
        def claim(_):
            barrier.wait()
            return self.store.claim_send('operation-1', 'hash')['new']
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, range(2))), [False, True])

    def test_same_key_different_hash_rejected(self):
        self.store.claim_send('operation-1', 'hash')
        with self.assertRaises(StateError):
            self.store.claim_send('operation-1', 'different')

    def test_pending_survives_restart_and_receipt_expiration(self):
        self.store.claim_send('operation-1', 'hash')
        self.store.close()
        self.store = Store(self.path, clock=lambda: self.now)
        self.assertFalse(self.store.claim_send('operation-1', 'hash')['new'])
        self.store.finish_send('operation-1', 'accepted', {'message_id': 'receipt'})
        self.assertEqual(self.store.claim_send('operation-1', 'hash')['receipt']['message_id'], 'receipt')
        self.now += 7 * 86400 + 1
        self.assertTrue(self.store.claim_send('operation-1', 'hash')['new'])

    def test_refresh_and_revocation_allow_omitted_resource_as_standard_oauth(self):
        tokens = self.exchange(self.code())
        fresh = self.oauth.exchange({'grant_type': 'refresh_token', 'client_id': 'client',
                                     'refresh_token': tokens['refresh_token']})
        self.oauth.revoke({'client_id': 'client', 'token': fresh['access_token']})
        with self.assertRaises(AuthError):
            self.oauth.authenticate(fresh['access_token'], 'whatsapp.read')

    def test_foreign_client_cannot_revoke_family_with_used_refresh(self):
        tokens = self.exchange(self.code())
        fresh = self.oauth.exchange({'grant_type': 'refresh_token', 'client_id': 'client',
                                     'refresh_token': tokens['refresh_token'], 'resource': self.params['resource']})
        self.oauth.clients['other'] = {'redirect_uris': ['https://other.example/callback']}
        with self.assertRaises(AuthError):
            self.oauth.exchange({'grant_type': 'refresh_token', 'client_id': 'other',
                                 'refresh_token': tokens['refresh_token'], 'resource': self.params['resource']})
        self.assertEqual(self.oauth.authenticate(fresh['access_token'], 'whatsapp.read').instance_id, 'fixed-instance')

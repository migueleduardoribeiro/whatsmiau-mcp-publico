"""Single-owner OAuth authorization-code/PKCE resource with opaque tokens."""
import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from mcp_store import StateError, digest


class AuthError(ValueError):
    def __init__(self, code='invalid_grant', status=400):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Principal:
    profile_id: str
    instance_id: str
    scopes: frozenset


class OAuth:
    def __init__(self, store, issuer, resource, clients, instance_id, allowed_user):
        self.store, self.issuer, self.resource = store, issuer.rstrip('/'), resource
        self.clients, self.instance_id, self.allowed_user = clients, instance_id, allowed_user

    def metadata(self):
        return {'issuer': self.issuer, 'authorization_endpoint': self.issuer + '/oauth/authorize',
                'token_endpoint': self.issuer + '/oauth/token',
                'revocation_endpoint': self.issuer + '/oauth/revoke',
                'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
                'code_challenge_methods_supported': ['S256'],
                'token_endpoint_auth_methods_supported': ['none', 'client_secret_post'],
                'scopes_supported': ['whatsapp.read', 'whatsapp.send']}

    def protected_resource(self):
        return {'resource': self.resource, 'authorization_servers': [self.issuer],
                'scopes_supported': ['whatsapp.read', 'whatsapp.send'],
                'bearer_methods_supported': ['header']}

    def _check_authorization(self, user, params):
        if user != self.allowed_user:
            raise AuthError('access_denied', 403)
        client = self.clients.get(params.get('client_id'))
        if not client or params.get('redirect_uri') not in client['redirect_uris']:
            raise AuthError('invalid_request')
        scopes = set(params.get('scope', '').split())
        if not scopes or scopes - {'whatsapp.read', 'whatsapp.send'}:
            raise AuthError('invalid_scope')
        if (params.get('response_type') != 'code' or params.get('resource') != self.resource
                or params.get('code_challenge_method') != 'S256'
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.get('code_challenge', ''))
                or len(params.get('state', '')) > 1024):
            raise AuthError('invalid_request')

    def prepare_consent(self, user, params):
        self._check_authorization(user, params)
        return self.store.ticket('consent', {'owner': user, 'params': params}, 300)

    def consent(self, user, ticket):
        try:
            pending = self.store.consume_ticket(ticket, 'consent', user)
        except StateError:
            raise AuthError('access_denied', 403) from None
        return self.authorize(user, pending['params'])

    def authorize(self, user, params):
        self._check_authorization(user, params)
        code = self.store.ticket('code', {'owner': user, 'params': params}, 60)
        return {'code': code, 'state': params.get('state', ''), 'redirect_uri': params['redirect_uri']}

    def _client(self, params, require_resource=False):
        client = self.clients.get(params.get('client_id'))
        if not client or not hmac.compare_digest(client.get('secret', ''), params.get('client_secret', '')):
            raise AuthError('invalid_client', 401)
        if (require_resource or 'resource' in params) and params.get('resource') != self.resource:
            raise AuthError('invalid_target')

    def _tokens(self, db, family):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('INSERT INTO tokens VALUES (?, ?, ?, ?, 0)',
                   (digest(access), 'access', family, self.store.clock() + 3600))
        db.execute('INSERT INTO tokens VALUES (?, ?, ?, ?, 0)',
                   (digest(refresh), 'refresh', family, self.store.clock() + 30 * 86400))
        return {'access_token': access, 'refresh_token': refresh, 'expires_in': 3600, 'token_type': 'Bearer'}

    def exchange(self, params):
        self._client(params, require_resource=params.get('grant_type') == 'authorization_code')
        if params.get('grant_type') == 'authorization_code':
            return self._exchange_code(params)
        if params.get('grant_type') == 'refresh_token':
            return self._refresh(params)
        raise AuthError('unsupported_grant_type')

    def _exchange_code(self, params):
        try:
            pending = self.store.consume_ticket(params.get('code', ''), 'code')
        except StateError:
            raise AuthError() from None
        original = pending['params']
        verifier = params.get('code_verifier', '')
        if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
            raise AuthError()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if (params.get('client_id') != original['client_id']
                or params.get('redirect_uri') != original['redirect_uri']
                or not hmac.compare_digest(challenge, original['code_challenge'])):
            raise AuthError()
        payload = {'client_id': original['client_id'], 'resource': self.resource,
                   'instance_id': self.instance_id, 'scopes': sorted(set(original['scope'].split())),
                   'profile_id': self.store.profile(pending['owner'])}
        with self.store.transaction() as db:
            family = secrets.token_urlsafe(24)
            db.execute('INSERT INTO families VALUES (?, ?, ?, 0)',
                       (family, json.dumps(payload), self.store.clock() + 30 * 86400))
            tokens = self._tokens(db, family)
        return dict(tokens, scope=' '.join(payload['scopes']))

    def _refresh(self, params):
        invalid = False
        with self.store.transaction() as db:
            row = db.execute('SELECT t.*, f.payload, f.expires AS family_expires, f.revoked FROM tokens t '
                             'JOIN families f ON f.id=t.family WHERE t.hash=? AND t.kind=?',
                             (digest(params.get('refresh_token', '')), 'refresh')).fetchone()
            if not row:
                invalid = True
            else:
                payload = json.loads(row['payload'])
                if payload['client_id'] != params['client_id']:
                    invalid = True
                elif row['used']:
                    # Commit revocation before raising; rollback would undo replay protection.
                    db.execute('UPDATE families SET revoked=1 WHERE id=?', (row['family'],))
                    invalid = True
                elif (row['revoked'] or min(row['expires'], row['family_expires']) <= self.store.clock()
                      or payload['client_id'] != params['client_id']
                      or payload['resource'] != self.resource or payload['instance_id'] != self.instance_id
                      or ('scope' in params and set(params['scope'].split()) != set(payload['scopes']))):
                    invalid = True
                else:
                    db.execute('UPDATE tokens SET used=1 WHERE hash=?', (row['hash'],))
                    tokens = dict(self._tokens(db, row['family']), scope=' '.join(payload['scopes']))
        if invalid:
            raise AuthError()
        return tokens

    def authenticate(self, token, required_scope):
        with self.store.transaction() as db:
            row = db.execute('SELECT t.expires, f.payload, f.expires AS family_expires, f.revoked FROM tokens t '
                             'JOIN families f ON f.id=t.family WHERE t.hash=? AND t.kind=?',
                             (digest(token), 'access')).fetchone()
            if not row or row['revoked'] or min(row['expires'], row['family_expires']) <= self.store.clock():
                raise AuthError('invalid_token', 401)
            payload = json.loads(row['payload'])
            if payload['resource'] != self.resource or payload['instance_id'] != self.instance_id:
                raise AuthError('invalid_token', 401)
            if required_scope and required_scope not in payload['scopes']:
                raise AuthError('insufficient_scope', 403)
            return Principal(payload['profile_id'], payload['instance_id'], frozenset(payload['scopes']))

    def revoke(self, params):
        self._client(params)
        with self.store.transaction() as db:
            row = db.execute('SELECT t.family, f.payload FROM tokens t JOIN families f ON f.id=t.family WHERE t.hash=?',
                             (digest(params.get('token', '')),)).fetchone()
            if row and json.loads(row['payload'])['client_id'] == params['client_id']:
                db.execute('UPDATE families SET revoked=1 WHERE id=?', (row['family'],))

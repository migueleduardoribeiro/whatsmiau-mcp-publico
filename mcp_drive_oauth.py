"""Private Google user authorization, distinct from the ChatGPT MCP OAuth."""
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from mcp_drive_transport import DriveError

SCOPE = 'https://www.googleapis.com/auth/drive.file'
CALLBACK = 'https://whatsmiau.example.org/drive/oauth/callback'


def private_json(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink(): raise DriveError('drive_invalid_path')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.new')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists(): temporary.unlink()


def read_private(path, default=None):
    path = Path(path)
    if not path.exists(): return default
    if path.is_symlink() or path.stat().st_size > 65536: raise DriveError('drive_invalid_credentials')
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError): raise DriveError('drive_invalid_credentials') from None


class DriveOAuth:
    def __init__(self, credentials_path, state_path, token_path, transport, clock=time.time):
        document = read_private(credentials_path, {})
        client = document.get('web', {})
        if (CALLBACK not in client.get('redirect_uris', []) or not client.get('client_id')
                or not client.get('client_secret')):
            raise DriveError('drive_invalid_credentials')
        self.client, self.state_path, self.token_path = client, Path(state_path), Path(token_path)
        self.transport, self.clock, self.lock = transport, clock, threading.RLock()
        existing_token = read_private(self.token_path, {})
        self.requires_authorization = not existing_token or existing_token.get('requires_authorization', False)

    def begin(self, owner: str) -> str:
        if not isinstance(owner, str) or not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}', owner):
            raise DriveError('drive_invalid_owner')
        with self.lock:
            states = read_private(self.state_path, {})
            states = {k: v for k, v in states.items() if v['expires'] > self.clock()}
            if len(states) >= 4: states.pop(min(states, key=lambda key: states[key]['expires']))
            raw, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            states[hashlib.sha256(raw.encode()).hexdigest()] = dict(owner=owner.lower(), verifier=verifier, expires=self.clock() + 600)
            private_json(self.state_path, states)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(dict(
                client_id=self.client['client_id'], redirect_uri=CALLBACK, response_type='code', scope=SCOPE,
                state=raw, code_challenge=challenge, code_challenge_method='S256',
                access_type='offline', prompt='consent', login_hint=owner))

    def _token(self, arguments):
        arguments.update(client_id=self.client['client_id'], client_secret=self.client['client_secret'])
        reply = self.transport.request('POST', '/token', headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                       body=urlencode(arguments).encode(), maximum=16384)
        data = reply.json()
        if reply.status != 200:
            if data.get('error') == 'invalid_grant':
                self.requires_authorization = True
                stored = read_private(self.token_path, {})
                if stored:
                    stored['requires_authorization'] = True
                    private_json(self.token_path, stored)
                raise DriveError('drive_authorization_required')
            raise DriveError('drive_authorization_failed', reply.status)
        if (not isinstance(data.get('access_token'), str) or not 1 <= len(data['access_token']) <= 8192
                or data.get('token_type') != 'Bearer' or type(data.get('expires_in')) is not int
                or not 60 <= data['expires_in'] <= 86400):
            raise DriveError('drive_invalid_response')
        if 'scope' in data and set(data['scope'].split()) != {SCOPE}:
            raise DriveError('drive_insufficient_scope')
        return data

    def complete(self, params: dict) -> None:
        raw = params.get('state')
        if not isinstance(raw, str) or not 20 <= len(raw) <= 200: raise DriveError('drive_invalid_state')
        with self.lock:
            states = read_private(self.state_path, {})
            state = states.pop(hashlib.sha256(raw.encode()).hexdigest(), None)
            private_json(self.state_path, states)
            if not state or state['expires'] <= self.clock(): raise DriveError('drive_invalid_state')
            code = params.get('code')
            if params.get('error') or not isinstance(code, str) or not 1 <= len(code) <= 4096:
                raise DriveError('drive_authorization_denied')
            token = self._token(dict(code=code, grant_type='authorization_code', redirect_uri=CALLBACK,
                                     code_verifier=state['verifier']))
            if token.get('scope') != SCOPE or not isinstance(token.get('refresh_token'), str) or not token['refresh_token']:
                raise DriveError('drive_insufficient_scope')
            account = self.transport.request('GET', '/api/drive/v3/about?fields=user',
                headers={'Authorization': 'Bearer ' + token['access_token']}, maximum=16384)
            if account.status != 200 or account.json().get('user', {}).get('emailAddress', '').lower() != state['owner']:
                raise DriveError('drive_wrong_owner')
            private_json(self.token_path, dict(access_token=token['access_token'], refresh_token=token['refresh_token'],
                                               expires=self.clock() + token['expires_in'], owner=state['owner'], scope=SCOPE))
            self.requires_authorization = False

    def access_token(self) -> str:
        with self.lock:
            token = read_private(self.token_path)
            if self.requires_authorization or not token: raise DriveError('drive_authorization_required')
            if token.get('scope') != SCOPE: raise DriveError('drive_insufficient_scope')
            if token['expires'] <= self.clock() + 60:
                reply = self._token(dict(grant_type='refresh_token', refresh_token=token['refresh_token']))
                token.update(access_token=reply['access_token'], expires=self.clock() + reply['expires_in'])
                if reply.get('refresh_token'): token['refresh_token'] = reply['refresh_token']
                private_json(self.token_path, token)
            return token['access_token']

    def health(self) -> dict:
        return {'requires_authorization': self.requires_authorization}

"""Small protected state for authorization and send receipts, never chat history."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class StateError(ValueError):
    pass


class Store:
    def __init__(self, path, clock=time.time):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('State must not be a symlink')
        # Create privately, including when the caller has a permissive umask.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name != 'nt':
            path.chmod(0o600)
        self.clock, self.lock = clock, threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS profiles (owner TEXT PRIMARY KEY, id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tickets
            (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS families
            (id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS tokens
            (hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS sends
            (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL);
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def close(self):
        with self.lock:
            self.db.close()

    def profile(self, owner):
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO profiles VALUES (?, ?)', (owner, secrets.token_urlsafe(24)))
            return db.execute('SELECT id FROM profiles WHERE owner=?', (owner,)).fetchone()['id']

    def ticket(self, kind, payload, ttl):
        raw = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute('DELETE FROM tickets WHERE expires <= ?', (self.clock(),))
            db.execute('INSERT INTO tickets VALUES (?, ?, ?, ?)',
                       (digest(raw), kind, json.dumps(payload), self.clock() + ttl))
        return raw

    def consume_ticket(self, raw, kind, owner=None):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tickets WHERE hash=? AND kind=?', (digest(raw), kind)).fetchone()
            if not row or row['expires'] <= self.clock():
                raise StateError('Expired or invalid ticket')
            payload = json.loads(row['payload'])
            if owner is not None and payload.get('owner') != owner:
                raise StateError('Wrong ticket owner')
            db.execute('DELETE FROM tickets WHERE hash=?', (digest(raw),))
            return payload

    def peek_ticket(self, raw, kind, owner=None):
        """Validate cursor bindings before consuming a one-use continuation."""
        with self.transaction() as db:
            row = db.execute('SELECT payload,expires FROM tickets WHERE hash=? AND kind=?', (digest(raw), kind)).fetchone()
            if not row or row['expires'] <= self.clock(): raise StateError('Expired or invalid ticket')
            payload = json.loads(row['payload'])
            if owner is not None and payload.get('owner') != owner: raise StateError('Wrong ticket owner')
            return payload

    def lookup_send(self, operation_id, request_hash):
        with self.transaction() as db:
            db.execute("DELETE FROM sends WHERE status='accepted' AND created < ?", (self.clock() - 7 * 86400,))
            row = db.execute('SELECT * FROM sends WHERE id=?', (operation_id,)).fetchone()
            if row is None:
                return None
            if row['request_hash'] != request_hash:
                raise StateError('Operation ID was used for another request')
            return {'new': False, 'status': row['status'], 'receipt': json.loads(row['receipt'])}

    def claim_send(self, operation_id, request_hash):
        with self.transaction() as db:
            # Unresolved requests are retained to prevent retries across outages.
            db.execute("DELETE FROM sends WHERE status='accepted' AND created < ?", (self.clock() - 7 * 86400,))
            row = db.execute('SELECT * FROM sends WHERE id=?', (operation_id,)).fetchone()
            if row:
                if row['request_hash'] != request_hash:
                    raise StateError('Operation ID was used for another request')
                return {'new': False, 'status': row['status'], 'receipt': json.loads(row['receipt'])}
            db.execute('INSERT INTO sends VALUES (?, ?, ?, ?, ?)',
                       (operation_id, request_hash, 'pending', '{}', self.clock()))
            return {'new': True, 'status': 'pending', 'receipt': {}}

    def finish_send(self, operation_id, status, receipt):
        if status not in ('accepted', 'uncertain'):
            raise StateError('Invalid send status')
        # Only normalized delivery receipts may be stored, never upstream payloads.
        allowed = {'message_id', 'status'}
        if not isinstance(receipt, dict) or set(receipt) - allowed:
            raise StateError('Invalid receipt')
        with self.transaction() as db:
            db.execute('UPDATE sends SET status=?, receipt=? WHERE id=?',
                       (status, json.dumps(receipt), operation_id))

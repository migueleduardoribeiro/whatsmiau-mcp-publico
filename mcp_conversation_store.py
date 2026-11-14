"""Bounded metadata/checkpoints and immutable presentation intents; no message bodies."""
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from mcp_archive_models import canonical, CHAT, INSTANCE
from mcp_conversation_models import Conversation, unknown, valid_ref, operation


class ConversationStore:
    def __init__(self, path, *, max_bytes=16777216, clock=time.time):
        path = Path(path)
        if path.is_symlink() or not 262144 <= max_bytes <= 16777216:
            raise ValueError('invalid_arguments')
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600); os.close(descriptor)
        self.clock, self.lock = clock, threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=3)
        if os.name != 'nt': path.chmod(0o600)
        self.db.execute('PRAGMA secure_delete=ON')
        size = self.db.execute('PRAGMA page_size').fetchone()[0]
        self.db.execute('PRAGMA max_page_count=' + str((max_bytes - 65536) // (2 * size)))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots(instance TEXT PRIMARY KEY, sequence INTEGER, data BLOB);
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, hash TEXT, result BLOB, created REAL);
        ''')
        if 'intent' not in {row[1] for row in self.db.execute('PRAGMA table_info(operations)')}:
            self.db.execute('ALTER TABLE operations ADD COLUMN intent BLOB')
        # A process restart loses continuity, even when the last checkpoint is recent.
        for instance, _, raw in self.db.execute('SELECT * FROM snapshots').fetchall():
            snap = self._unknown(json.loads(raw), 'process_restarted')
            self.db.execute('UPDATE snapshots SET data=? WHERE instance=?', (canonical(snap), instance))
        self.db.commit()

    def close(self):
        with self.lock: self.db.close()

    @staticmethod
    def _unknown(snap, reason):
        snap.update(complete=False, continuous=False, reason=reason)
        for row in snap['conversations']:
            row.update(unread_count=None, unread_refs=None, marked_unread=None, read_state=unknown(reason))
        return snap

    def apply(self, snapshot):
        instance = snapshot.get('instance')
        rows = snapshot.get('conversations')
        sequence = snapshot.get('sequence')
        if (not isinstance(instance, str) or not INSTANCE.fullmatch(instance)
                or not isinstance(rows, list) or len(rows) > 10000
                or type(sequence) is not int or sequence < 0):
            raise ValueError('invalid_arguments')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or set(row) != set(Conversation.__annotations__)
                    or not isinstance(row.get('chat'), str) or not CHAT.fullmatch(row['chat'])
                    or row['chat'] in seen or not isinstance(row.get('aliases'), list)
                    or row['chat'] not in row['aliases'] or len(row['aliases']) > 2
                    or any(not isinstance(jid, str) or not CHAT.fullmatch(jid) for jid in row['aliases'])
                    or (row['unread_count'] is not None and (type(row['unread_count']) is not int or row['unread_count'] < 0))):
                raise ValueError('invalid_arguments')
            seen.add(row['chat'])
            for ref in row['unread_refs'] or []:
                valid_ref(ref, instance)
                if ref['chat'] not in row['aliases'] or ref['from_me']: raise ValueError('invalid_arguments')
        snap = json.loads(canonical(snapshot))
        if snapshot.get('continuous') is not True: self._unknown(snap, 'continuity_unverified')
        with self.lock, self.db:
            old = self.db.execute('SELECT sequence FROM snapshots WHERE instance=?', (instance,)).fetchone()
            if old and old[0] >= sequence: return
            self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)', (instance, sequence, canonical(snap)))

    def snapshot(self, instance):
        with self.lock:
            row = self.db.execute('SELECT data FROM snapshots WHERE instance=?', (instance,)).fetchone()
        return json.loads(row[0]) if row else dict(conversations=[], cursor=None, complete=False,
            revision=None, observed_at=None, reason='baseline_unavailable')

    def invalidate(self, instance: str, reason: str):
        with self.lock, self.db:
            snap = self.snapshot(instance)
            self._unknown(snap, reason)
            self.db.execute('UPDATE snapshots SET data=? WHERE instance=?', (canonical(snap), instance))

    @staticmethod
    def _key(profile, instance, operation_id):
        operation(operation_id)
        return hashlib.sha256(canonical([profile, instance, operation_id])).hexdigest()

    def claim(self, profile, instance, operation_id, intent):
        key = self._key(profile, instance, operation_id)
        signature = hashlib.sha256(canonical(intent)).hexdigest()
        with self.lock, self.db:
            row = self.db.execute('SELECT hash,result FROM operations WHERE id=?', (key,)).fetchone()
            if row:
                if row[0] != signature: raise ValueError('operation_conflict')
                return json.loads(row[1]) if row[1] else dict(status='uncertain',items=[],
                    operation_id=operation_id,reason='presentation_in_progress')
            if self.db.execute('SELECT count(*) FROM operations').fetchone()[0] >= 10000:
                raise ValueError('conversation_state_full')
            self.db.execute('INSERT INTO operations(id,hash,result,created,intent) VALUES (?,?,NULL,?,?)',
                            (key, signature, self.clock(), canonical(intent)))
        return None

    def load(self, profile, instance, operation_id):
        with self.lock:
            row = self.db.execute('SELECT intent,result FROM operations WHERE id=?',
                                  (self._key(profile, instance, operation_id),)).fetchone()
        return None if not row else dict(intent=json.loads(row[0]), result=json.loads(row[1]) if row[1]
            else dict(status='uncertain', items=[], operation_id=operation_id, reason='presentation_in_progress'))

    def finish(self, profile, instance, operation_id, result):
        with self.lock, self.db:
            self.db.execute('UPDATE operations SET result=? WHERE id=?',
                            (canonical(result), self._key(profile, instance, operation_id)))

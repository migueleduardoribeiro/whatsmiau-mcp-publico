"""Small durable operational outbox, never a permanent conversation database."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from mcp_archive_models import canonical, event_identity


class ArchiveFull(Exception):
    pass


class ArchiveStore:
    def __init__(self, path: Path, *, max_bytes=33554432, max_events=10000, clock=time.time):
        self.path, self.clock = Path(path), clock
        if max_bytes < 262144 or not 1 <= max_events <= 10000:
            raise ValueError('archive_invalid_budget')
        self.max_bytes, self.max_events = max_bytes, max_events
        self.lock = threading.RLock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('archive_invalid_path')
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=3)
        if os.name != 'nt': self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA secure_delete=ON')
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        # Reserve a full rollback journal plus header/sector overhead. This is
        # deliberately conservative: payload capacity is below the physical cap.
        pages = (max_bytes - 65536) // (2 * page_size)
        if self.path.stat().st_size > pages * page_size:
            self.db.close()
            raise ArchiveFull('archive_queue_budget')
        self.db.execute('PRAGMA max_page_count=' + str(pages))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT UNIQUE NOT NULL,
                payload BLOB NOT NULL, accepted REAL NOT NULL, batch TEXT);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, manifest BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS seen(identity TEXT PRIMARY KEY, confirmed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                with self.db: yield self.db
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_FULL:
                    raise ArchiveFull('archive_queue_budget') from None
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def state(self, key, value=None):
        if key not in ('capture_started_at', 'last_committed_at', 'known_gaps', 'root_id', 'pending_capture'):
            raise ValueError('archive_invalid_state')
        with self.transaction() as db:
            if value is not None:
                encoded = json.dumps(value, allow_nan=False)
                if len(encoded) > 4096: raise ValueError('archive_state_limit')
                db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, encoded))
            row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def _gap(self):
        # One bounded summary, no conversation data and no unbounded outage log.
        try: self.state('known_gaps', [{'reason': 'capture_failed', 'last_observed_at': self.clock()}])
        except (ArchiveFull, sqlite3.Error): pass

    def accept(self, events: list[dict]) -> int:
        if len(events) > 1000: raise ValueError('archive_event_limit')
        try:
            with self.transaction() as db:
                db.execute('DELETE FROM seen WHERE confirmed < ?', (self.clock() - 7 * 86400,))
                count = db.execute('SELECT count(*) FROM events').fetchone()[0]
                added = 0
                for event in events:
                    identity, payload = event_identity(event), canonical(event)
                    if len(payload) > 262144: raise ValueError('archive_event_limit')
                    if (db.execute('SELECT 1 FROM events WHERE identity=?', (identity,)).fetchone()
                            or db.execute('SELECT 1 FROM seen WHERE identity=?', (identity,)).fetchone()):
                        continue
                    if count + added >= self.max_events: raise ArchiveFull('archive_queue_count')
                    db.execute('INSERT INTO events(identity,payload,accepted) VALUES (?,?,?)',
                               (identity, payload, self.clock()))
                    added += 1
                if added:
                    db.execute('INSERT OR IGNORE INTO state VALUES (?,?)', ('capture_started_at', json.dumps(self.clock())))
                return added
        except ArchiveFull:
            self._gap()
            raise

    def pending(self, limit: int) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100: raise ValueError('archive_invalid_limit')
        with self.transaction() as db:
            rows = db.execute('SELECT seq,payload,accepted FROM events WHERE batch IS NULL ORDER BY seq LIMIT ?', (limit,))
            return [dict(json.loads(row['payload']), _seq=row['seq'], _accepted=row['accepted']) for row in rows]

    def batch_events(self, batch_id):
        with self.transaction() as db:
            return [dict(json.loads(row['payload']), _seq=row['seq']) for row in db.execute(
                'SELECT seq,payload FROM events WHERE batch=? ORDER BY seq', (batch_id,))]

    def local_files(self):
        with self.transaction() as db:
            return {row[0] for row in db.execute("SELECT json_extract(payload,'$.media.local_file') FROM events") if row[0]}

    def prepare(self, batch_id: str, manifest: dict) -> None:
        encoded = canonical(manifest)
        seqs = manifest.get('seqs', [])
        if (not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 100 or len(encoded) > 1048576
                or not isinstance(seqs, list) or not 1 <= len(seqs) <= 100
                or any(type(seq) is not int for seq in seqs) or len(set(seqs)) != len(seqs)):
            raise ValueError('archive_invalid_batch')
        with self.transaction() as db:
            old = db.execute('SELECT manifest FROM batches WHERE id=?', (batch_id,)).fetchone()
            if old:
                if old[0] != encoded: raise ValueError('archive_batch_conflict')
                return
            for seq in seqs:
                row = db.execute('SELECT batch FROM events WHERE seq=?', (seq,)).fetchone()
                if row is None or row[0] is not None: raise ValueError('archive_batch_conflict')
            db.execute('INSERT INTO batches VALUES (?,?)', (batch_id, encoded))
            db.executemany('UPDATE events SET batch=? WHERE seq=?', [(batch_id, seq) for seq in seqs])

    def update_manifest(self, batch_id, manifest):
        encoded = canonical(manifest)
        if len(encoded) > 1048576: raise ValueError('archive_manifest_limit')
        with self.transaction() as db:
            previous = db.execute('SELECT manifest FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not previous or json.loads(previous[0])['seqs'] != manifest.get('seqs'):
                raise ValueError('archive_batch_conflict')
            db.execute('UPDATE batches SET manifest=? WHERE id=?', (encoded, batch_id))

    def prepared(self):
        with self.transaction() as db:
            return [dict(json.loads(row['manifest']), batch_id=row['id'])
                    for row in db.execute('SELECT * FROM batches ORDER BY rowid LIMIT 1')]

    def confirm(self, batch_id: str) -> None:
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM batches WHERE id=?', (batch_id,)).fetchone():
                raise ValueError('archive_unknown_batch')
            db.execute('INSERT OR REPLACE INTO seen SELECT identity,? FROM events WHERE batch=?', (self.clock(), batch_id))
            db.execute('DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen ORDER BY confirmed DESC LIMIT -1 OFFSET 10000)')
            db.execute('DELETE FROM events WHERE batch=?', (batch_id,))
            db.execute('DELETE FROM batches WHERE id=?', (batch_id,))
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('last_committed_at', json.dumps(self.clock())))

    def health(self) -> dict:
        with self.transaction() as db:
            count, oldest = db.execute('SELECT count(*),min(accepted) FROM events').fetchone()
            prepared = db.execute('SELECT count(*) FROM batches').fetchone()[0]
            state = {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM state')}
        return {'pending_events': count, 'oldest_pending_at': oldest,
                'prepared_batches': prepared,
                'capture_started_at': state.get('capture_started_at'), 'last_committed_at': state.get('last_committed_at'),
                'known_gaps': state.get('known_gaps', []), 'pending_capture': state.get('pending_capture', False)}

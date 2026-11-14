"""Durable conservative reservations. No provider retry is inferred from a failure."""
import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LIMITS = {'rpm': 20, 'rpd': 2000, 'ash': 7200, 'asd': 28800}
WINDOWS = {'rpm': 60, 'rpd': 86400, 'ash': 3600, 'asd': 86400}


class TranscriptionError(ValueError):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def reserved_seconds(duration):
    if type(duration) not in (float, int) or not math.isfinite(duration) or not 0 < duration <= 1800:
        raise TranscriptionError('audio_limit_exceeded')
    return math.ceil(max(duration, 10)) + 1


class TranscriptionStore:
    def __init__(self, path, *, clock=time.time, max_bytes=8388608, limits=None):
        self.path, self.clock = Path(path), clock
        self.limits = dict(LIMITS)
        for k, v in (limits or {}).items():
            if k not in LIMITS or type(v) is not int or not 1 <= v <= LIMITS[k]:
                raise ValueError('invalid_transcription_limits')
            self.limits[k] = v
        if not 1048576 <= max_bytes <= 8388608 or self.path.is_symlink():
            raise ValueError('invalid_transcription_state')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        if os.name != 'nt': self.path.chmod(0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=3, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA secure_delete=ON')
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        pages = (max_bytes - 65536) // (2 * page_size)
        if self.path.stat().st_size > pages * page_size:
            self.db.close(); raise TranscriptionError('transcription_unavailable')
        self.db.execute('PRAGMA max_page_count=' + str(pages))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, key TEXT NOT NULL, at REAL NOT NULL, seconds INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,status TEXT NOT NULL,created REAL NOT NULL,
                updated REAL NOT NULL,drive_id TEXT NOT NULL,result BLOB);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS capacity(key TEXT PRIMARY KEY,padding BLOB NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield self.db
                self.db.commit()
            except Exception as error:
                self.db.rollback()
                if isinstance(error, sqlite3.Error):
                    raise TranscriptionError('transcription_unavailable') from None
                raise

    def _now(self, db):
        row = db.execute("SELECT value FROM settings WHERE key='clock'").fetchone()
        now = max(self.clock(), row[0] if row else 0)
        db.execute("INSERT OR REPLACE INTO settings VALUES ('clock',?)", (now,))
        return now

    def _clean(self, db, now):
        db.execute('DELETE FROM usage WHERE at<=?', (now - 86400,))
        db.execute("DELETE FROM jobs WHERE updated<=? AND result IS NULL", (now - 86400,))
        db.execute('DELETE FROM capacity WHERE key NOT IN (SELECT key FROM jobs)')

    def claim(self, key, duration, *, drive_id):
        seconds = reserved_seconds(duration)
        if (not re.fullmatch(r'[a-f0-9]{64}', key)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', drive_id)):
            raise ValueError('invalid_transcription_identity')
        with self.transaction() as db:
            now = self._now(db); self._clean(db, now)
            previous = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            if previous and previous['status'] != 'failed': return dict(self._job(previous), new=False)
            if db.execute('SELECT 1 FROM jobs WHERE result IS NOT NULL').fetchone():
                raise TranscriptionError('transcription_unavailable')
            if db.execute('SELECT 1 FROM capacity').fetchone():
                raise TranscriptionError('transcription_unavailable')
            block = db.execute("SELECT value FROM settings WHERE key='blocked_until'").fetchone()
            if block and block[0] > now:
                raise TranscriptionError('provider_rate_limited', math.ceil(block[0] - now))
            rows = db.execute('SELECT at,seconds FROM usage ORDER BY at').fetchall()
            waits = []
            for dimension, maximum in self.limits.items():
                window = WINDOWS[dimension]
                relevant = [r for r in rows if r['at'] > now - window]
                audio = dimension in ('ash', 'asd')
                total = sum(r['seconds'] if audio else 1 for r in relevant)
                requested = seconds if audio else 1
                if total + requested <= maximum: continue
                wait = window
                for row in relevant:
                    total -= row['seconds'] if audio else 1
                    if total + requested <= maximum:
                        wait = math.ceil(row['at'] + window - now); break
                waits.append(wait)
            if waits: raise TranscriptionError('quota_exceeded', max(waits))
            # Reserve durable space for the largest result BEFORE any provider send.
            db.execute('INSERT INTO capacity VALUES (?,zeroblob(270336))', (key,))
            db.execute('INSERT INTO usage(key,at,seconds) VALUES (?,?,?)', (key, now, seconds))
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,NULL)', (key, 'reserved', now, now, drive_id))
            return dict(self._job(db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()), new=True)

    @staticmethod
    def _job(row):
        return dict(row, result=json.loads(row['result']) if row['result'] is not None else None)

    def get(self, key):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            return self._job(row) if row else None

    def _transition(self, key, status, *, result=None):
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode() if result is not None else None
        if encoded and len(encoded) > 262144: raise TranscriptionError('transcription_unavailable')
        with self.transaction() as db:
            now = self._now(db)
            previous = db.execute('SELECT status FROM jobs WHERE key=?', (key,)).fetchone()
            permitted = {'submitted': ('reserved',), 'uncertain': ('reserved', 'submitted', 'uncertain'),
                         'result_pending_drive': ('submitted',), 'completed': ('result_pending_drive', 'completed'),
                         'failed': ('reserved', 'submitted')}
            if not previous or previous[0] not in permitted[status]:
                raise TranscriptionError('transcription_unavailable')
            if status in ('failed','uncertain','result_pending_drive'):
                db.execute('DELETE FROM capacity WHERE key=?', (key,))
            db.execute('UPDATE jobs SET status=?,updated=?,result=? WHERE key=?', (status, now, encoded, key))

    def submitted(self, key): self._transition(key, 'submitted')
    def uncertain(self, key): self._transition(key, 'uncertain')
    def failed(self, key): self._transition(key, 'failed')
    def save_result(self, key, result): self._transition(key, 'result_pending_drive', result=result)
    def completed(self, key): self._transition(key, 'completed')

    def block(self, seconds):
        seconds = min(86400, max(1, int(seconds)))
        with self.transaction() as db:
            now = self._now(db)
            old = db.execute("SELECT value FROM settings WHERE key='blocked_until'").fetchone()
            db.execute("INSERT OR REPLACE INTO settings VALUES ('blocked_until',?)", (max(now + seconds, old[0] if old else 0),))

    def pending_result(self):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE result IS NOT NULL LIMIT 1').fetchone()
            return self._job(row) if row else None

    def health(self):
        with self.transaction() as db:
            now = self._now(db); self._clean(db, now)
            used = {}
            for dimension, window in WINDOWS.items():
                field = 'SUM(seconds)' if dimension in ('ash', 'asd') else 'COUNT(*)'
                used[dimension] = db.execute('SELECT COALESCE(' + field + ',0) FROM usage WHERE at>?', (now - window,)).fetchone()[0]
            row = db.execute("SELECT value FROM settings WHERE key='blocked_until'").fetchone()
            return {'used': used, 'limits': dict(self.limits), 'remaining': {k: max(0, self.limits[k]-v) for k,v in used.items()},
                    'retry_after': max(0, math.ceil(row[0]-now)) if row else 0,
                    'pending_result': bool(db.execute('SELECT 1 FROM jobs WHERE result IS NOT NULL').fetchone())}

    def close(self):
        with self.lock: self.db.close()

"""Single cancellable writer; an accepted Google upload is reconciled by ID."""
import hashlib
import io
import secrets
import time
import threading
from datetime import datetime, timezone
from mcp_archive_models import canonical
from mcp_archive_catalog import ArchiveCatalog, index_record, bound_index, index_current
from mcp_drive_transport import DriveError


class ArchiveWorker:
    def __init__(self, store, drive, media, *, transfer_slot, clock=time.time):
        self.store, self.drive, self.media, self.clock = store, drive, media, clock
        self.transfer_slot, self.catalog = transfer_slot, ArchiveCatalog(drive)
        self.last_error = None
        self.index_lock, self.index_jobs = threading.Lock(), []
        self.index_error = None

    def index_health(self):
        with self.index_lock:
            return {'index_jobs_pending': len(self.index_jobs), 'index_error': self.index_error}

    def schedule_index(self, instance, chat):
        with self.index_lock:
            item = (instance, chat)
            if item not in self.index_jobs and len(self.index_jobs) < 100:
                self.index_jobs.append(item)

    def index_step(self):
        with self.index_lock:
            if not self.index_jobs: return False
            instance, chat = self.index_jobs[0]
        folder, manifest = self.catalog.manifest(instance, chat)
        index = manifest.get('time_index')
        if index is not None and not index_current(manifest):
            manifest.pop('time_index'); index = None
        if index is not None and index.get('complete') is not True and not isinstance(index.get('pending'), list):
            manifest.pop('time_index'); index = None
        if not folder or manifest.get('time_index_disabled') or (index and index.get('complete') is True):
            with self.index_lock: self.index_jobs.pop(0)
            return True
        if index is None:
            index = {'version': 2, 'complete': False, 'slots': {},
                     'coverage_revision': manifest.get('coverage', {}).get('last_committed_at'),
                     'pending': [[prefix, identifier] for prefix, identifier in manifest.get('buckets', {}).items()]}
            manifest['time_index'] = index
        deadline = time.monotonic() + 8
        processed = 0
        while index['pending'] and processed < 8 and time.monotonic() < deadline:
            prefix, identifier = index['pending'].pop()
            node = self.catalog.read_node(identifier)
            processed += 1
            if 'children' in node:
                index['pending'].extend([prefix, child] for child in node['children'].values())
            else:
                for record in node.get('records', {}).values():
                    if not index_record(index, prefix, record):
                        manifest.pop('time_index', None); manifest['time_index_disabled'] = True
                        break
            if 'time_index' not in manifest: break
        if 'time_index' in manifest and not index['pending']:
            index['complete'] = True
            index.pop('pending')
        bound_index(manifest)
        self.catalog._write(folder, 'current-v1', manifest, lambda _: self.drive.create_id())
        if manifest.get('time_index_disabled') or manifest.get('time_index', {}).get('complete'):
            with self.index_lock: self.index_jobs.pop(0)
        return True

    def _root(self):
        root = self.store.state('root_id')
        if root: self.drive.root_id = root
        else:
            root = self.drive.create_folder('LylaHub WhatsMiau', None, 'archive-root-v1')
            self.store.state('root_id', root)

    def step(self):
        ready = self.store.prepared()
        if ready:
            manifest = ready[0]
        else:
            pending = self.store.pending(100)
            if not pending: return False
            first = pending[0]
            rows, length = [], 0
            for item in pending:
                if (item['instance'], item['chat']) != (first['instance'], first['chat']): break
                size = len(canonical(item)) + 1
                if length + size > 1048000: break
                rows.append(item); length += size
            if (len(rows) < 100 and length < 1040000 and self.clock() - first['_accepted'] < 60
                    and not any(item['media'] for item in rows)):
                return False
            self._root()
            manifest = dict(batch_id=secrets.token_hex(16), seqs=[item['_seq'] for item in rows],
                            drive_id=self.drive.create_id(), files={}, media_results={}, media_pending={})
            self.store.prepare(manifest['batch_id'], {k: v for k, v in manifest.items() if k != 'batch_id'})
        self._root()
        batch_id = manifest['batch_id']

        def persist(): self.store.update_manifest(batch_id, {k: v for k, v in manifest.items() if k != 'batch_id'})
        def claim(identity):
            if identity not in manifest['files']:
                manifest['files'][identity] = self.drive.create_id(); persist()
            return manifest['files'][identity]

        rows = self.store.batch_events(batch_id)
        if not rows: raise DriveError('archive_invalid_batch')
        first = rows[0]
        folder = self.catalog.chat_folder(first['instance'], first['chat'], create=True)
        timestamp = first.get('timestamp') or first['observed_at']
        day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        day_folder = self.drive.create_folder(day, folder, 'day:' + day)
        records = []
        for item in rows:
            record = {key: value for key, value in item.items() if not key.startswith('_') and key != 'media'}
            record['order'] = item['_seq']
            record['media'] = None
            if item['media']:
                key = str(item['_seq'])
                if key not in manifest['media_results']:
                    identifier = claim('media-' + key)
                    pending = manifest.setdefault('media_pending', {}).get(key)
                    if pending:
                        try: existing = self.drive.metadata(identifier)
                        except DriveError as error:
                            if error.status != 404: raise
                        else:
                            self._verify(existing, pending['size'], pending['sha256'])
                            manifest['media_results'][key] = dict(pending, drive_id=identifier)
                            persist()
                if key not in manifest['media_results']:
                    with self.media.prepare(item) as output:
                        safe = {k: output[k] for k in ('state', 'kind', 'mime_type', 'filename', 'size', 'sha256') if k in output}
                        if output['state'] == 'archived':
                            manifest['media_pending'][key] = safe; persist()
                            self._put_once(identifier, day_folder, 'attachment-' + key + '.bin',
                                           output['source'], output['size'], output['sha256'],
                                           output.get('mime_type', 'application/octet-stream'))
                            safe['drive_id'] = identifier
                        manifest['media_results'][key] = safe; persist()
                record['media'] = manifest['media_results'][key]
            records.append(record)
        payload = b''.join(canonical(item) + b'\n' for item in records)
        if len(payload) > 1048576: raise DriveError('archive_batch_limit')
        self._put_once(manifest['drive_id'], day_folder, 'batch-' + batch_id + '.jsonl',
                       io.BytesIO(payload), len(payload), hashlib.sha256(payload).hexdigest(), 'application/x-ndjson')
        health = self.store.health()
        coverage = {key: health[key] for key in ('capture_started_at', 'known_gaps')}
        coverage['last_committed_at'] = self.clock()
        self.catalog.update(first['instance'], first['chat'], records, claim, coverage)
        self.store.confirm(batch_id)
        if hasattr(self.media, 'confirm'):
            for row in rows: self.media.confirm(row)
        self.last_error = None
        return True

    def _put_once(self, identifier, folder, name, source, size, digest, mime_type):
        try: existing = self.drive.metadata(identifier)
        except DriveError as error:
            if error.status != 404: raise
        else:
            self._verify(existing, size, digest)
            return existing
        metadata = {'name': name, 'mimeType': mime_type, 'appProperties': {'sha256': digest}}
        return self.drive.put(identifier, folder, metadata, source, size)

    @staticmethod
    def _verify(existing, size, digest):
        if (existing.get('appProperties', {}).get('sha256') != digest or str(existing.get('size')) != str(size)
                or existing.get('sha256Checksum', digest) != digest):
            raise DriveError('archive_upload_conflict')

    def run(self, stop, maintenance=None):
        delay = 1
        while not stop.is_set():
            if maintenance:
                try: maintenance()
                except Exception: pass
            try:
                worked = self.step(); delay = 1
            except Exception:
                # Safe category only. No exception text, Google body or chat IDs.
                self.last_error = 'archive_upload_pending'
                stop.wait(delay); delay = min(delay * 2, 60)
                continue
            if not worked:
                try:
                    indexed = self.index_step()
                    with self.index_lock: self.index_error = None
                    if not indexed: stop.wait(1)
                except Exception:
                    # Index rebuilding is optional and must not report an upload failure.
                    with self.index_lock: self.index_error = 'archive_index_pending'
                    stop.wait(5)

"""Archive composition in the existing WhatsMiau panel process only."""
import hashlib
import http.client
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from mcp_archive_models import normalize_event, canonical
from mcp_archive_store import ArchiveStore, ArchiveFull
from mcp_archive_media import ArchiveMedia
from mcp_archive_worker import ArchiveWorker
from mcp_archive_query import ArchiveQuery
from mcp_drive_transport import DriveTransport, DriveError
from mcp_drive_oauth import DriveOAuth
from mcp_drive_client import DriveClient


class ArchiveRuntime:
    def __init__(self, config, bridge, mcp_store, media_provider):
        self.config, self.settings, self.bridge, self.mcp_store = config, dict(config['archive']), bridge, mcp_store
        self.instance = config['instance_id']
        if (not isinstance(self.settings.get('hook_token'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', self.settings['hook_token'])
                or any(type(self.settings.get(key, False)) is not bool
                       for key in ('capture_enabled', 'relay_previous', 'trusted_view_once_metadata'))):
            raise ValueError('archive_invalid_configuration')
        directory = Path(self.settings['directory']).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.spool = directory / 'spool'; self.spool.mkdir(mode=0o700, exist_ok=True)
        self.transfer_slot = threading.BoundedSemaphore(1)
        self.transport = DriveTransport(self.settings['proxy_token'])
        self.oauth = DriveOAuth(self.settings['credentials_path'], directory / 'oauth-state.json',
                                directory / 'google-tokens.json', self.transport)
        self.drive = DriveClient(self.oauth, self.transport)
        self.store = ArchiveStore(directory / 'queue.db')
        referenced = self.store.local_files()
        for path in self.spool.iterdir():
            if not path.is_symlink() and path.is_file() and path.name not in referenced and re.fullmatch(
                    r'(accepted-[a-f0-9]{64}\.bin|[a-f0-9]{32}\.new|transcription-[A-Za-z0-9_-]+\.audio)', path.name):
                path.unlink()
        self.drive.root_id = self.store.state('root_id')
        self.media = ArchiveMedia(media_provider, transfer_slot=self.transfer_slot, spool=self.spool)
        self.worker = ArchiveWorker(self.store, self.drive, self.media, transfer_slot=self.transfer_slot)
        self.query = ArchiveQuery(self.drive, cursor_store=mcp_store, health=self.store.health,
                                  schedule_index=self.worker.schedule_index)
        self.transcriber, self.transcription_error = None, None
        settings = config.get('transcription')
        if isinstance(settings, dict) and settings.get('enabled') is True:
            transcription_store = None
            try:
                from mcp_audio_source import AudioSource
                from mcp_groq import GroqClient
                from mcp_transcription_store import TranscriptionStore
                from mcp_transcription_cache import TranscriptionCache
                from mcp_transcription import Transcriber
                if not settings.get('free_plan_verified_at'): raise ValueError('free_plan_unverified')
                client = GroqClient(settings['key_path'], settings['proxy_token'])
                transcription_store = TranscriptionStore(directory / 'transcription.db', limits=settings.get('limits'))
                self.transcriber = Transcriber(self.query, AudioSource(self.drive, self.spool), client,
                    transcription_store, TranscriptionCache(self.drive), transfer_slot=self.transfer_slot)
            except Exception:
                if transcription_store is not None: transcription_store.close()
                self.transcription_error = 'transcription_configuration_unavailable'
        self.stop_event, self.thread, self.closed = threading.Event(), None, False

    def start(self):
        if self.closed: raise ValueError('archive_closed')
        if self.thread and self.thread.is_alive(): return
        self.thread = threading.Thread(target=self.worker.run,
            args=(self.stop_event, self.transcriber.flush_pending if self.transcriber else None), name='drive-archive', daemon=True)
        self.thread.start()

    def stop(self):
        if self.closed: return
        self.closed = True; self.stop_event.set()
        if self.thread: self.thread.join(5)
        if not self.thread or not self.thread.is_alive():
            self.store.close()
            if self.transcriber: self.transcriber.store.close()

    def health(self):
        health = self.store.health()
        health.update(self.oauth.health(), capture_enabled=self.settings.get('capture_enabled', False),
                      worker_running=bool(self.thread and self.thread.is_alive()), upload_state=self.worker.last_error)
        health.update(self.worker.index_health())
        health['transcription'] = (dict(self.transcriber.health(), configured=True) if self.transcriber else
                                   {'configured':False,'last_error':self.transcription_error})
        return health

    def accept_webhook(self, instance, body):
        if instance != self.instance or len(body) > 262144: raise ValueError('archive_invalid_event')
        failure, added = None, 0
        try:
            payload = json.loads(body)
            events = normalize_event(payload, instance)
            if not self.settings.get('capture_enabled'): raise DriveError('archive_capture_disabled')
            permitted = []
            originals = payload['data'] if payload.get('event') == 'messages.set' else [payload.get('data', {})]
            known_flags = {item.get('key', {}).get('id'): item.get('isViewOnce')
                           for item in originals if isinstance(item, dict)}
            for event in events:
                if event['media'] and (not self.settings.get('trusted_view_once_metadata')
                                       or known_flags.get(event['message_id']) is not False):
                    self.store._gap()
                    continue
                permitted.append(event)
            added = self.store.accept(permitted)
        except Exception as error:
            try: self.store._gap()
            except Exception: pass
            failure = error
        if self.settings.get('relay_previous'):
            try: self._relay(body)
            except Exception:
                self.store._gap()
                if failure is None: failure = DriveError('archive_relay_failed')
        if failure: raise failure
        return {'status': 'queued' if added else 'duplicate_or_ignored'}

    def _relay(self, body):
        connection = http.client.HTTPConnection('127.0.0.1', 8094, timeout=5)
        try:
            connection.request('POST', '/relay/' + self.instance, body=body,
                headers={'Content-Type': 'application/json', 'X-LylaHub-Drive-IP': '127.0.0.1',
                         'X-LylaHub-Drive-Token': self.settings['proxy_token']})
            response = connection.getresponse()
            data = response.read(65537)
            if not 200 <= response.status < 300 or len(data) > 65536: raise DriveError('archive_relay_failed')
        except (OSError, http.client.HTTPException): raise DriveError('archive_relay_failed') from None
        finally: connection.close()

    def record_accepted_send(self, send):
        if not self.settings.get('capture_enabled'): return {'capture': 'disabled'}
        local = None
        try:
            if send['instance'] != self.instance: raise ValueError('archive_invalid_event')
            timestamp = int(send.get('timestamp') or time.time())
            payload = {'instance': self.instance, 'event': 'messages.upsert', 'data': {
                'key': {'id': send['message_id'], 'remoteJid': send['chat'], 'fromMe': True},
                'messageTimestamp': timestamp, 'message': {'conversation': send.get('text', '')}}}
            event = normalize_event(payload, self.instance)[0]
            event['source'] = 'mcp'
            data = send.get('data')
            if data is not None:
                if not isinstance(data, bytes) or len(data) > 10485760: raise ValueError('archive_invalid_file')
                name = 'accepted-' + hashlib.sha256(canonical([self.instance, send['chat'], send['message_id']])).hexdigest() + '.bin'
                local = self.spool / name
                if not local.exists():
                    used = sum(path.stat().st_size for path in self.spool.iterdir() if path.is_file())
                    if used + len(data) > 20971520: raise ArchiveFull('archive_spool_full')
                    temporary = self.spool / (secrets.token_hex(16) + '.new')
                    try:
                        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
                        temporary.replace(local)
                    finally:
                        if temporary.exists(): temporary.unlink()
                event['media'] = {'kind': send.get('media_kind', 'document'), 'fileLength': str(len(data)),
                    'local_file': name, 'mimetype': send.get('mime_type', 'application/octet-stream'),
                    'fileName': str(send.get('filename', ''))[:255], 'sha256': hashlib.sha256(data).hexdigest()}
                event['revision'] = hashlib.sha256(canonical({'kind': event['kind'], 'text': event['text'],
                                                               'media': event['media']})).hexdigest()
            self.store.accept([event])
            if local and local.name not in self.store.local_files(): local.unlink(missing_ok=True)
            return {'capture': 'queued'}
        except Exception:
            try: self.store.state('pending_capture', True); self.store._gap()
            except Exception: pass
            # Never raise through an already accepted WhatsApp send receipt.
            return {'capture': 'pending'}

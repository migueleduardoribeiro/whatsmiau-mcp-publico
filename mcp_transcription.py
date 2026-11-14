"""On-demand inference; durable reservations precede every provider submission."""
import hashlib
import time
from contextlib import nullcontext
from mcp_archive_models import canonical
from mcp_drive_transport import DriveError
from mcp_groq import GroqRejected, MODEL
from mcp_transcription_store import TranscriptionError


class Transcriber:
    def __init__(self, query, audio_source, client, store, cache, *, transfer_slot, clock=time.monotonic):
        self.query, self.audio_source, self.client = query, audio_source, client
        self.store, self.cache, self.transfer_slot, self.clock = store, cache, transfer_slot, clock
        self.last_error = None

    def _scope(self):
        transport = getattr(self.cache.drive, 'transport', None)
        return transport.scope(65) if transport else nullcontext()

    @staticmethod
    def _public(result, message_id, source, pending=False):
        return {'status': 'completed_pending_archive' if pending else 'completed',
                'message_id': message_id, 'text': result['text'], 'model': MODEL,
                'duration_seconds': result['duration'], 'source': source}

    def transcribe(self, profile, instance, chats, message_id, language='auto'):
        if language != 'auto' and (not isinstance(language, str) or len(language) != 2
                                    or not language.isascii() or not language.islower() or not language.isalpha()):
            raise TranscriptionError('invalid_language')
        deadline = self.clock() + 65
        def reference():
            if self.clock() >= deadline: raise TranscriptionError('transcription_unavailable')
            return self.query.audio_reference(profile, instance, chats, message_id)
        with self._scope():
            ref = reference()
            key = hashlib.sha256(canonical([instance, ref['sha256'], MODEL, language])).hexdigest()
            def validate():
                current = reference()
                if current['sha256'] != ref['sha256']:
                    raise TranscriptionError('audio_reference_unavailable')
            def lookup():
                try: return self.cache.lookup(key)
                except DriveError:
                    local = self.store.get(key)
                    if local and local['result']:
                        validate()
                        return local['result']
                    raise
            cached = lookup()
            if cached:
                validate()
                local = self.store.get(key)
                return self._public(cached, message_id, 'local_pending' if local and local['result'] else 'drive_cache',
                                    bool(local and local['result']))
            if not self.transfer_slot.acquire(blocking=False): raise TranscriptionError('busy')
            try:
                cached = lookup()
                if cached:
                    validate()
                    return self._public(cached, message_id, 'drive_cache')
                previous = self.store.get(key)
                if previous and previous['result']:
                    validate()
                    return self._public(previous['result'], message_id, 'local_pending', True)
                with self.audio_source.prepare(ref, deadline=deadline) as audio:
                    validate()
                    job = self.store.claim(key, audio['duration'], drive_id=self.cache.drive.create_id())
                    if not job['new']: raise TranscriptionError('transcription_uncertain', 86400)
                    self.store.submitted(key)
                    try:
                        output = self.client.transcribe(audio['source'], size=audio['size'],
                            extension=audio['extension'], mime_type=audio['mime_type'], language=language, deadline=deadline)
                    except GroqRejected as error:
                        self.store.failed(key)
                        if error.code == 'provider_rate_limited': self.store.block(error.retry_after or 60)
                        raise
                    except Exception:
                        self.store.uncertain(key)
                        raise TranscriptionError('transcription_uncertain') from None
                    result = dict(output, key=key, v=1, duration=audio['duration'], language=language,
                                  audio_sha256=ref['sha256'])
                    self.store.save_result(key, result)
                pending = False
                try:
                    self.cache.commit(key, result, drive_id=job['drive_id'])
                    self.store.completed(key)
                except (DriveError, TranscriptionError): pending = True
                validate()
                return self._public(result, message_id, 'groq', pending)
            finally: self.transfer_slot.release()

    def flush_pending(self):
        if not self.transfer_slot.acquire(blocking=False): return False
        try:
            pending = self.store.pending_result()
            if not pending: return False
            with self._scope():
                self.cache.commit(pending['key'], pending['result'], drive_id=pending['drive_id'])
            self.store.completed(pending['key'])
            self.last_error = None
            return True
        except (DriveError, TranscriptionError):
            self.last_error = 'transcription_archive_unavailable'
            return False
        finally: self.transfer_slot.release()

    def health(self): return dict(self.store.health(), last_error=self.last_error)

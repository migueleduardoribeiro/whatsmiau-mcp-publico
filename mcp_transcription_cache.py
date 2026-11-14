"""Private immutable Drive transcripts, reconciled with preallocated IDs."""
import hashlib
import io
import json
from mcp_drive_transport import DriveError
from mcp_archive_models import canonical


class TranscriptionCache:
    def __init__(self, drive): self.drive = drive

    def _folder(self, create=False):
        if create:
            return self.drive.create_folder('Transcriptions', self.drive.root_id, 'transcriptions-v1')
        item = self.drive.lookup(self.drive.root_id, 'transcriptions-v1')
        return item['id'] if item else None

    def lookup(self, key):
        folder = self._folder()
        item = self.drive.lookup(folder, 'transcription:' + key) if folder else None
        if not item: return None
        data = self.drive.read(item['id'], 262144)
        if (str(len(data)) != str(item.get('size'))
                or hashlib.sha256(data).hexdigest() != item.get('sha256Checksum')):
            raise DriveError('transcription_cache_invalid')
        try:
            result = json.loads(data)
            if (not isinstance(result, dict) or result.get('key') != key
                    or result.get('model') != 'whisper-large-v3'
                    or not isinstance(result.get('text'), str)):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise DriveError('transcription_cache_invalid') from None

    def commit(self, key, result, *, drive_id):
        data = canonical(result)
        if len(data) > 262144: raise DriveError('transcription_cache_limit')
        digest = hashlib.sha256(data).hexdigest()
        try: existing = self.drive.metadata(drive_id)
        except DriveError as error:
            if error.status != 404: raise
        else:
            if existing.get('sha256Checksum') != digest or str(existing.get('size')) != str(len(data)):
                raise DriveError('transcription_cache_conflict')
            return
        uploaded = self.drive.put(drive_id, self._folder(create=True),
                       {'name': 'transcript-' + key + '.json', 'mimeType': 'application/json',
                        'appProperties': {'archiveIdentity': 'transcription:' + key}}, io.BytesIO(data), len(data))
        if (uploaded.get('id') != drive_id or str(uploaded.get('size')) != str(len(data))
                or uploaded.get('sha256Checksum') != digest):
            raise DriveError('transcription_cache_conflict')

"""Bounded private audio spool. Packet durations avoid trusting header duration."""
import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from mcp_transcription_store import TranscriptionError, reserved_seconds

FORMATS = {'ogg': ('ogg','audio/ogg'), 'mp3': ('mp3','audio/mpeg'), 'wav': ('wav','audio/wav'),
           'flac': ('flac','audio/flac'), 'mov': ('m4a','audio/mp4'), 'matroska': ('webm','audio/webm'),
           'webm': ('webm','audio/webm'), 'mpeg': ('mpeg','audio/mpeg')}


def probe_audio(path, *, executable='/usr/bin/ffprobe', timeout=5):
    command = [executable, '-v', 'error', '-protocol_whitelist', 'file',
               '-format_whitelist', 'ogg,mp3,wav,flac,mov,matroska,webm,mpeg',
               '-select_streams', 'a:0', '-show_entries', 'packet=duration_time:format=format_name:stream=codec_name',
               '-of', 'json', str(Path(path).resolve())]
    process = None
    state = {'duration': 0.0, 'packets': 0, 'format': None, 'invalid': False}
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={}, shell=False)
        def scan():
            total = 0
            try:
                for line in iter(lambda: process.stdout.readline(4097), b''):
                    total += len(line)
                    if len(line) > 4096 or total > 33554432: raise ValueError()
                    value = line.strip().rstrip(b',')
                    if not value.startswith((b'"duration_time"', b'"format_name"')): continue
                    item = json.loads(b'{' + value + b'}')
                    if 'format_name' in item: state['format'] = str(item['format_name']).split(',')[0]
                    if 'duration_time' in item:
                        duration = float(item['duration_time'])
                        if not math.isfinite(duration) or duration <= 0: raise ValueError()
                        state['duration'] += duration; state['packets'] += 1
                        if state['duration'] > 1800.01 or state['packets'] > 250000: raise ValueError()
            except Exception:
                state['invalid'] = True
                try: process.kill()
                except OSError: pass
        reader = threading.Thread(target=scan, daemon=True)
        reader.start()
        try: status = process.wait(timeout=min(5, max(.01, timeout)))
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=1)
            raise TranscriptionError('audio_duration_unavailable') from None
        finally: reader.join(1)
        if (reader.is_alive() or status != 0 or state['invalid'] or state['format'] not in FORMATS
                or not state['packets']): raise TranscriptionError('audio_duration_unavailable')
        reserved_seconds(state['duration'])
        extension, mime = FORMATS[state['format']]
        return dict(duration=state['duration'], format=state['format'], extension=extension, mime_type=mime)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise TranscriptionError('audio_duration_unavailable') from None
    finally:
        if process and process.stdout: process.stdout.close()


class AudioSource:
    def __init__(self, drive, spool, *, probe=probe_audio):
        self.drive, self.spool, self.probe = drive, Path(spool), probe

    @contextmanager
    def prepare(self, reference, *, deadline=None):
        def remaining():
            value = 5 if deadline is None else min(5, deadline-time.monotonic())
            if value <= 0: raise TranscriptionError('transcription_unavailable')
            return value
        remaining()
        size = reference.get('size')
        if type(size) is not int or not 0 < size <= 10485760: raise TranscriptionError('audio_limit_exceeded')
        if self.spool.is_symlink(): raise TranscriptionError('transcription_unavailable')
        used = sum(p.stat().st_size for p in self.spool.iterdir() if p.is_file())
        if used + size > 20971520: raise TranscriptionError('transcription_unavailable')
        fd, filename = tempfile.mkstemp(prefix='transcription-', suffix='.audio', dir=self.spool)
        path = Path(filename)
        try:
            with os.fdopen(fd, 'w+b') as stream:
                count = self.drive.download(reference['drive_id'], stream, size)
                stream.flush(); stream.seek(0)
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if count != size or not hmac.compare_digest(digest, reference['sha256']):
                    raise TranscriptionError('media_integrity_failed')
                probe_timeout = remaining()
                result = self.probe(path, timeout=probe_timeout) if deadline is not None else self.probe(path)
                reserved_seconds(result['duration']); stream.seek(0)
                yield dict(result, path=path, source=stream, size=size, sha256=digest)
        finally: path.unlink(missing_ok=True)

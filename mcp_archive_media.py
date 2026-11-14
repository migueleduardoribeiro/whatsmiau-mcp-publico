"""Authenticate encrypted WhatsApp media, then decrypt in-place in one spool."""
import base64
import hashlib
import hmac
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp_drive_transport import DriveError
from mcp_files import FileError

# WhatsMeow 9b528b0345e4 download.go MediaType definitions.
LABELS = {'image': b'WhatsApp Image Keys', 'sticker': b'WhatsApp Image Keys',
          'document': b'WhatsApp Document Keys', 'audio': b'WhatsApp Audio Keys',
          'video': b'WhatsApp Video Keys', 'ptv': b'WhatsApp Video Keys'}


def key(descriptor, field):
    value = base64.b64decode(descriptor[field], validate=True)
    if len(value) != 32: raise ValueError('invalid_key')
    return value


def decrypt_in_place(stream, descriptor, kind, length):
    expanded = HKDF(algorithm=hashes.SHA256(), length=112, salt=None, info=LABELS[kind]).derive(key(descriptor, 'mediaKey'))
    stream.seek(0, 2); total = stream.tell()
    if total != (length // 16 + 1) * 16 + 10: raise ValueError('invalid_size')
    stream.seek(0); encrypted_hash = hashlib.sha256(); mac = hmac.new(expanded[48:80], expanded[:16], hashlib.sha256)
    remaining = total - 10
    while remaining:
        chunk = stream.read(min(65536, remaining)); remaining -= len(chunk)
        if not chunk: raise ValueError('truncated_ciphertext')
        encrypted_hash.update(chunk); mac.update(chunk)
    trailer = stream.read(10); encrypted_hash.update(trailer)
    if (not hmac.compare_digest(encrypted_hash.digest(), key(descriptor, 'fileEncSha256'))
            or not hmac.compare_digest(mac.digest()[:10], trailer)):
        raise ValueError('invalid_integrity')
    decryptor = Cipher(algorithms.AES(expanded[16:48]), modes.CBC(expanded[:16])).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    read_offset = written = 0; plain_hash = hashlib.sha256()
    while read_offset < total - 10:
        stream.seek(read_offset); chunk = stream.read(min(65536, total - 10 - read_offset)); read_offset += len(chunk)
        plain = unpadder.update(decryptor.update(chunk))
        stream.seek(written); stream.write(plain); written += len(plain); plain_hash.update(plain)
    final = unpadder.update(decryptor.finalize()) + unpadder.finalize()
    stream.seek(written); stream.write(final); written += len(final); plain_hash.update(final)
    if written != length or not hmac.compare_digest(plain_hash.digest(), key(descriptor, 'fileSha256')):
        raise ValueError('invalid_integrity')
    stream.truncate(written); stream.flush(); stream.seek(0)
    return plain_hash.hexdigest()


class ArchiveMedia:
    def __init__(self, transport, *, transfer_slot, spool: Path, maximum_spool=20971520):
        self.transport, self.slot, self.spool = transport, transfer_slot, Path(spool)
        self.maximum_spool = maximum_spool
        self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def prepare(self, event):
        if not self.slot.acquire(False): raise DriveError('busy')
        stream = None
        try:
            descriptor = event.get('media') or {}
            kind = descriptor.get('kind')
            try: size = int(descriptor.get('fileLength', '0'))
            except (TypeError, ValueError): size = 0
            maximum = 5242880 if kind == 'image' else 10485760
            if not (0 if kind == 'document' else 1) <= size <= maximum:
                result = {'state': 'size_limit', 'kind': kind}
            elif descriptor.get('local_file'):
                try:
                    path = self._local(descriptor['local_file'])
                    if path.stat().st_size != size: raise ValueError()
                    stream = path.open('rb')
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest(); stream.seek(0)
                    if not hmac.compare_digest(digest, descriptor['sha256']): raise ValueError()
                    result = {'state': 'archived', 'kind': kind, 'source': stream, 'size': size, 'sha256': digest,
                              'mime_type': descriptor.get('mimetype', 'application/octet-stream'),
                              'filename': descriptor.get('fileName', '')[:255]}
                except (ValueError, OSError, KeyError): result = {'state': 'media_unavailable', 'kind': kind}
            elif kind not in LABELS or descriptor.get('viewOnce'):
                result = {'state': 'media_unavailable', 'kind': kind}
            else:
                try:
                    used = sum(path.stat().st_size for path in self.spool.iterdir() if path.is_file())
                    if used + size + 26 > self.maximum_spool: raise DriveError('archive_spool_full')
                    stream = tempfile.TemporaryFile(mode='w+b', dir=self.spool)
                    self.transport.download_to(descriptor.get('url'), 'whatsapp', size + 26, stream)
                    digest = decrypt_in_place(stream, descriptor, kind, size)
                    result = {'state': 'archived', 'kind': kind, 'source': stream, 'size': size, 'sha256': digest,
                              'mime_type': descriptor.get('mimetype', 'application/octet-stream'),
                              'filename': descriptor.get('fileName', '')[:255]}
                except (DriveError, FileError, ValueError, KeyError, TypeError, OSError):
                    result = {'state': 'media_unavailable', 'kind': kind}
            yield result
        finally:
            if stream: stream.close()
            self.slot.release()

    def _local(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'accepted-[a-f0-9]{64}\.bin', name): raise ValueError()
        path = self.spool / name
        if path.is_symlink() or path.resolve().parent != self.spool.resolve(): raise ValueError()
        return path

    def confirm(self, event):
        local = (event.get('media') or {}).get('local_file')
        if local:
            try: self._local(local).unlink(missing_ok=True)
            except OSError: pass  # startup cleanup reconciles unreferenced private spool files

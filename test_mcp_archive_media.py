import base64
import hashlib
import hmac
import tempfile
import threading
import unittest
from pathlib import Path
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp_archive_media import ArchiveMedia
from mcp_drive_transport import DriveError


def encrypted_fixture(data, kind='document'):
    key = b'k' * 32
    labels = {'document': b'WhatsApp Document Keys', 'image': b'WhatsApp Image Keys',
              'audio': b'WhatsApp Audio Keys', 'video': b'WhatsApp Video Keys'}
    expanded = HKDF(algorithm=hashes.SHA256(), length=112, salt=None, info=labels[kind]).derive(key)
    padder = padding.PKCS7(128).padder(); padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(expanded[16:48]), modes.CBC(expanded[:16])).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    encrypted = ciphertext + hmac.new(expanded[48:80], expanded[:16] + ciphertext, hashlib.sha256).digest()[:10]
    descriptor = {'kind': kind, 'url': 'https://mmg.whatsapp.net/fixture', 'fileLength': str(len(data)),
                  'mediaKey': base64.b64encode(key).decode(), 'fileName': 'source.py'}
    descriptor.update({field: base64.b64encode(hashlib.sha256(value).digest()).decode()
                       for field, value in [('fileSha256', data), ('fileEncSha256', encrypted)]})
    return encrypted, descriptor


class Download:
    def __init__(self, value): self.value = value; self.calls = 0
    def download_to(self, url, route, maximum, target):
        self.calls += 1
        if len(self.value) > maximum: raise DriveError('media_limit')
        for offset in range(0, len(self.value), 4096): target.write(self.value[offset:offset+4096])


class ArchiveMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.slot = threading.BoundedSemaphore(1)
    def test_media_integrity_and_limits(self):
        for kind in ('document', 'image', 'audio', 'video'):
            data = b'opaque bytes\x00' * 20000
            ciphertext, descriptor = encrypted_fixture(data, kind)
            provider = ArchiveMedia(Download(ciphertext), transfer_slot=self.slot, spool=Path(self.temp.name))
            with provider.prepare({'media': descriptor}) as result:
                self.assertEqual(result['state'], 'archived')
                self.assertEqual(result['source'].read(), data)
                self.assertLessEqual(sum(p.stat().st_size for p in Path(self.temp.name).iterdir()), 20971520)
            self.assertEqual(list(Path(self.temp.name).iterdir()), [])
    def test_corruption_and_large_file_never_expose_bytes(self):
        ciphertext, descriptor = encrypted_fixture(b'private')
        provider = ArchiveMedia(Download(ciphertext[:-1] + b'x'), transfer_slot=self.slot, spool=Path(self.temp.name))
        with provider.prepare({'media': descriptor}) as result: self.assertEqual(result['state'], 'media_unavailable')
        descriptor['fileLength'] = str(10485761)
        with provider.prepare({'media': descriptor}) as result: self.assertEqual(result['state'], 'size_limit')
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
    def test_single_large_transfer(self):
        ciphertext, descriptor = encrypted_fixture(b'file')
        provider = ArchiveMedia(Download(ciphertext), transfer_slot=self.slot, spool=Path(self.temp.name))
        self.slot.acquire()
        try:
            with self.assertRaisesRegex(DriveError, 'busy'):
                with provider.prepare({'media': descriptor}): pass
        finally: self.slot.release()


if __name__ == '__main__': unittest.main()

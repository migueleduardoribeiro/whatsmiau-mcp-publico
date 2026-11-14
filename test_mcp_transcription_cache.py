import unittest
from mcp_transcription_cache import TranscriptionCache
from mcp_drive_transport import DriveError
from test_mcp_archive_worker import FakeDrive


class CacheTests(unittest.TestCase):
    def test_new_upload_ack_must_match_id_hash_size(self):
        from unittest.mock import Mock
        drive=FakeDrive(); cache=TranscriptionCache(drive)
        result={'key':'a'*64,'text':'hello','model':'whisper-large-v3','duration':1}
        drive.put=Mock(return_value={'id':'wrong','size':'2','sha256Checksum':'wrong'})
        with self.assertRaises(DriveError): cache.commit(result['key'],result,drive_id='preallocated')
    def test_integrity_and_id_reconciliation(self):
        drive=FakeDrive(); cache=TranscriptionCache(drive)
        result={'key':'a'*64,'text':'hello','model':'whisper-large-v3','duration':1}
        identifier=drive.create_id()
        cache.commit(result['key'],result,drive_id=identifier)
        cache.commit(result['key'],result,drive_id=identifier)
        self.assertEqual(drive.uploaded.count(identifier),1)
        self.assertEqual(cache.lookup(result['key']),result)
        drive.bytes[identifier]=b'{}'
        with self.assertRaises(DriveError): cache.lookup(result['key'])


if __name__=='__main__': unittest.main()

import hashlib
import io
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
from mcp_transcription import Transcriber
from mcp_transcription_cache import TranscriptionCache
from mcp_transcription_store import TranscriptionStore, TranscriptionError
from mcp_drive_transport import DriveError
from mcp_groq import GroqRejected
from test_mcp_archive_worker import FakeDrive


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='.'); self.addCleanup(self.temp.cleanup)
        self.now=100000; self.path=Path(self.temp.name)/'state.db'
        self.store=TranscriptionStore(self.path,clock=lambda:self.now); self.addCleanup(self.store.close)
        self.drive=FakeDrive(); self.cache=TranscriptionCache(self.drive)
        self.query=Mock(); self.query.audio_reference.return_value={'chat':'15551234567@s.whatsapp.net','message_id':'one',
            'drive_id':'audio','size':5,'sha256':hashlib.sha256(b'audio').hexdigest(),'mime_type':'audio/ogg'}
        class Source:
            @contextmanager
            def prepare(inner,reference,*,deadline=None):
                yield dict(source=io.BytesIO(b'audio'),size=5,duration=1,extension='ogg',mime_type='audio/ogg',sha256=reference['sha256'])
        self.source=Source(); self.client=Mock(); self.client.transcribe.return_value={'text':'recognized','model':'whisper-large-v3'}
        self.slot=threading.BoundedSemaphore(1)
        self.service=Transcriber(self.query,self.source,self.client,self.store,self.cache,transfer_slot=self.slot)
    def call(self): return self.service.transcribe('owner','personal',{'15551234567@s.whatsapp.net'},'one')
    def test_cache_hit_does_not_infer(self):
        self.assertEqual(self.call()['source'],'groq')
        self.assertEqual(self.call()['source'],'drive_cache')
        self.assertEqual(self.client.transcribe.call_count,1)
        self.assertEqual(self.store.health()['used']['rpm'],1)
        self.assertNotIn('drive_id',str(self.call()))
    def test_simultaneous_same_audio_calls_once(self):
        entered, release = threading.Event(), threading.Event()
        def provider(*args, **kwargs):
            entered.set(); release.wait(5)
            return {'text':'recognized','model':'whisper-large-v3'}
        self.client.transcribe.side_effect=provider
        outputs=[]
        worker=threading.Thread(target=lambda:outputs.append(self.call()))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(TranscriptionError,'busy'): self.call()
        finally: release.set(); worker.join(5)
        self.assertEqual(len(outputs),1)
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_shared_slot_and_quota_before_provider(self):
        self.slot.acquire()
        with self.assertRaisesRegex(TranscriptionError,'busy'): self.call()
        self.slot.release(); self.store.block(10)
        with self.assertRaisesRegex(TranscriptionError,'provider_rate_limited'): self.call()
        self.client.transcribe.assert_not_called()
    def test_timeout_and_restart_do_not_resubmit(self):
        self.client.transcribe.side_effect=TranscriptionError('transcription_uncertain')
        with self.assertRaisesRegex(TranscriptionError,'transcription_uncertain'): self.call()
        with self.assertRaisesRegex(TranscriptionError,'transcription_uncertain'): self.call()
        again=TranscriptionStore(self.path,clock=lambda:self.now); self.addCleanup(again.close)
        self.service.store=again
        with self.assertRaisesRegex(TranscriptionError,'transcription_uncertain'): self.call()
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_lost_drive_ack_and_pending_reuse(self):
        original=self.drive.put
        def fail(*args):
            original(*args)
            if args[2]['name'].startswith('transcript-'): raise DriveError('lost_ack')
        self.drive.put=fail
        first=self.call()
        self.assertEqual(first['status'],'completed_pending_archive')
        self.assertTrue(self.store.pending_result())
        self.drive.put=original
        self.assertEqual(self.call()['text'],'recognized')
        self.assertEqual(self.client.transcribe.call_count,1)
        self.assertTrue(self.service.flush_pending())
        self.assertFalse(self.store.pending_result())
    def test_pending_maintenance_does_not_infer(self):
        original=self.cache.commit; self.cache.commit=Mock(side_effect=DriveError())
        self.call(); self.cache.commit=original
        self.assertTrue(self.service.flush_pending())
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_local_pending_survives_remote_cache_outage(self):
        self.cache.commit=Mock(side_effect=DriveError())
        self.call()
        self.cache.lookup=Mock(side_effect=DriveError())
        self.assertEqual(self.call()['text'],'recognized')
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_capacity_exhaustion_blocks_before_provider(self):
        pages=self.store.db.execute('PRAGMA page_count').fetchone()[0]
        self.store.db.execute('PRAGMA max_page_count='+str(pages))
        with self.assertRaises(TranscriptionError): self.call()
        self.client.transcribe.assert_not_called()
    def test_failed_result_save_blocks_other_audio_after_restart(self):
        self.store.save_result=Mock(side_effect=TranscriptionError('transcription_unavailable'))
        with self.assertRaises(TranscriptionError): self.call()
        again=TranscriptionStore(self.path,clock=lambda:self.now); self.addCleanup(again.close)
        self.service.store=again
        self.query.audio_reference.return_value=dict(self.query.audio_reference.return_value,sha256='b'*64)
        with self.assertRaisesRegex(TranscriptionError,'transcription_unavailable'): self.call()
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_deleted_during_cache_lookup_is_denied(self):
        self.call()
        ref=self.query.audio_reference.return_value
        self.query.audio_reference.side_effect=[ref,TranscriptionError('audio_reference_unavailable')]
        with self.assertRaisesRegex(TranscriptionError,'audio_reference_unavailable'): self.call()
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_failed_local_save_never_claims_success(self):
        self.store.save_result=Mock(side_effect=TranscriptionError('transcription_unavailable'))
        with self.assertRaises(TranscriptionError): self.call()
        with self.assertRaisesRegex(TranscriptionError,'transcription_uncertain'): self.call()
        self.assertEqual(self.client.transcribe.call_count,1)
    def test_known_429_can_retry_after_block_without_erasing_usage(self):
        self.client.transcribe.side_effect=[GroqRejected('provider_rate_limited',5),{'text':'recognized','model':'whisper-large-v3'}]
        with self.assertRaisesRegex(TranscriptionError,'provider_rate_limited'): self.call()
        with self.assertRaisesRegex(TranscriptionError,'provider_rate_limited'): self.call()
        self.assertEqual(self.client.transcribe.call_count,1)
        self.now+=6; self.assertEqual(self.call()['status'],'completed')
        self.assertEqual(self.store.health()['used']['rpm'],2)


if __name__=='__main__': unittest.main()

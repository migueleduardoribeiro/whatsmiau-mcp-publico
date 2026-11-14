import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from mcp_audio_source import AudioSource, probe_audio
from mcp_transcription_store import TranscriptionError
from mcp_drive_transport import DriveTransport
from mcp_drive_client import DriveClient
from test_mcp_drive_client import OAuth
from test_mcp_archive_query import ArchiveQueryTests


class AudioSourceTests(unittest.TestCase):
    def test_expired_tool_deadline_does_not_start_probe(self):
        import time
        class Drive:
            def download(self,identifier,target,maximum): target.write(b'a'); return 1
        with tempfile.TemporaryDirectory(dir='.') as name:
            probe=Mock()
            source=AudioSource(Drive(),name,probe=probe)
            with self.assertRaises(TranscriptionError):
                with source.prepare({'drive_id':'id','size':1,'sha256':hashlib.sha256(b'a').hexdigest()},deadline=time.monotonic()-1): pass
            probe.assert_not_called()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.spool = Path(self.temp.name)
    def test_download_hash_size_cleanup_and_cap(self):
        class Drive:
            def download(self, identifier, target, maximum): target.write(b'audio'); return 5
        reference = dict(drive_id='id', size=5, sha256=hashlib.sha256(b'audio').hexdigest())
        source = AudioSource(Drive(), self.spool, probe=lambda path: {'duration': 2, 'format':'ogg','extension':'ogg','mime_type':'audio/ogg'})
        with source.prepare(reference) as output:
            self.assertEqual(output['source'].read(), b'audio')
            self.assertEqual(output['duration'], 2)
        self.assertEqual(list(self.spool.iterdir()), [])
        with self.assertRaisesRegex(TranscriptionError,'media_integrity_failed'):
            with source.prepare(dict(reference, sha256='0'*64)): pass
        with self.assertRaises(TranscriptionError):
            with source.prepare(dict(reference, size=10485761)): pass
        (self.spool/'occupied').write_bytes(b'x' * 20971520)
        with self.assertRaises(TranscriptionError):
            with source.prepare(reference): pass
    def fake_probe(self, format='ogg', durations=('0.02','0.02')):
        process=Mock(); process.returncode=0
        text='\n'.join('"duration_time": "'+d+'",' for d in durations)
        text+='\n"format_name": "'+format+'",\n"codec_name": "opus"\n'
        process.stdout=io.BytesIO(text.encode()); process.wait.return_value=0
        return process
    def test_packet_duration_supported_formats_and_no_network(self):
        for format in ('ogg','mp3','wav','flac','mov,mp4,m4a,3gp,3g2,mj2','matroska,webm'):
            with patch('mcp_audio_source.subprocess.Popen',return_value=self.fake_probe(format)) as spawn:
                result=probe_audio(self.spool/'local')
                self.assertAlmostEqual(result['duration'],.04)
                args=spawn.call_args.args[0]
                self.assertIn('-protocol_whitelist',args)
                self.assertNotIn('http',args)
                self.assertEqual(spawn.call_args.kwargs['env'],{})
    def test_playlist_unknown_duration_and_duration_limit(self):
        for format,durations in [('hls',('1',)),('ogg',('N/A',)),('ogg',('1800.1',)),('ogg',('nan',))]:
            with patch('mcp_audio_source.subprocess.Popen',return_value=self.fake_probe(format,durations)):
                with self.assertRaises(TranscriptionError): probe_audio(self.spool/'local')
    def test_probe_timeout(self):
        process=self.fake_probe(); process.wait.side_effect=[subprocess.TimeoutExpired('probe',5),0]
        with patch('mcp_audio_source.subprocess.Popen',return_value=process):
            with self.assertRaises(TranscriptionError): probe_audio(self.spool/'local')
        process.kill.assert_called()
    def test_stream_transport_has_no_body_copy(self):
        reply=Mock(); reply.status=200; reply.getheaders.return_value=[('Content-Length','6')]
        reply.getheader.side_effect=lambda name,default=None: {'Content-Length':'6'}.get(name,default)
        reply.read1.side_effect=[b'abc',b'def',b'']
        connection=Mock(); connection.getresponse.return_value=reply
        with patch('mcp_drive_transport.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]), patch('mcp_drive_transport.http.client.HTTPConnection',return_value=connection):
            target=io.BytesIO()
            response=DriveTransport('x'*48).request('GET','/api/drive/v3/files/id?alt=media',sink=target)
        self.assertEqual(response.data,b''); self.assertEqual(target.getvalue(),b'abcdef')
    def test_client_download_checks_private_tree_and_size(self):
        transport=Mock(); drive=DriveClient(OAuth(),transport)
        drive.metadata=Mock(return_value={'size':'3','mimeType':'audio/ogg'})
        def request(method,path,**kwargs):
            kwargs['sink'].write(b'abc'); return Mock(status=200)
        transport.request.side_effect=request
        out=io.BytesIO(); self.assertEqual(drive.download('id',out,3),3)
        self.assertEqual(out.getvalue(),b'abc')


class AudioReferenceTests(unittest.TestCase):
    setUp = ArchiveQueryTests.setUp
    seed = ArchiveQueryTests.seed
    def test_audio_alias_and_delete(self):
        self.seed(); record=self.worker.catalog.current('personal',self.chat,'m0')
        lid='15559999999@lid'
        record=dict(record,chat=lid,media={'kind':'audio','state':'archived','drive_id':'audio-id','size':3,'sha256':'a'*64,'mime_type':'audio/ogg'})
        self.worker.catalog.update('personal',lid,[record],lambda key:self.drive.create_id(),{})
        ref=self.query.audio_reference('owner','personal',{self.chat,lid},'m0')
        self.assertEqual(ref['drive_id'],'audio-id')
        record=dict(record,kind='delete',order=100)
        self.worker.catalog.update('personal',lid,[record],lambda key:self.drive.create_id(),{})
        with self.assertRaisesRegex(TranscriptionError,'audio_reference_unavailable'):
            self.query.audio_reference('owner','personal',{self.chat,lid},'m0')


if __name__=='__main__': unittest.main()

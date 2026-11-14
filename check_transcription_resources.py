"""Linux synthetic whole-cgroup check, no external inference or private audio."""
import hashlib
import io
import json
import tempfile
import threading
import time
import wave
from pathlib import Path
from unittest.mock import Mock, patch
from mcp_audio_source import AudioSource, probe_audio
from mcp_groq import GroqClient
from mcp_archive_catalog import ArchiveCatalog
from mcp_archive_query import ArchiveQuery
from mcp_store import Store
from mcp_transcription_store import TranscriptionStore
from test_mcp_archive_worker import FakeDrive
import server  # Include panel/server baseline in the measured process.
from mcp_archive_runtime import ArchiveRuntime
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def main():
    Cipher(algorithms.AES(b'x'*32),modes.CBC(b'x'*16)).decryptor()
    with tempfile.TemporaryDirectory(dir='.') as name:
        directory=Path(name); spool=directory/'spool'; spool.mkdir()
        audio=directory/'synthetic.wav'
        with wave.open(str(audio),'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            for _ in range(327): stream.writeframesraw(b'\0'*32000)
        # Exact maximum file size, valid trailing bytes ignored by WAV decoder.
        with audio.open('ab') as stream:
            stream.write(b'\0'*(10485760-audio.stat().st_size))
        with audio.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        class Download:
            def download(self,identifier,target,maximum):
                with audio.open('rb') as stream:
                    while chunk:=stream.read(65536): target.write(chunk)
                return target.tell()
        cache=[bytes([i])*1048576 for i in range(8)]
        cursor_store=Store(directory/'cursor.db')
        pending_store=TranscriptionStore(directory/'transcription.db')
        pending_store.claim('a'*64,1,drive_id='pending'); pending_store.submitted('a'*64)
        pending_store.save_result('a'*64,{'text':'p'*250000})
        drive=FakeDrive()
        records=[dict(v=1,instance='fixture',chat='15557654321@s.whatsapp.net',message_id='stress-'+str(i),
                      timestamp=1792245600,observed_at=1792245600,kind='upsert',text='"'*16384,
                      media=None,from_me=False,source='webhook',order=i) for i in range(100)]
        ArchiveCatalog(drive).update('fixture','15557654321@s.whatsapp.net',records,lambda k:drive.create_id(),{})
        del records
        archive_query=ArchiveQuery(drive,cursor_store=cursor_store)
        stop=threading.Event(); counts=[0]
        def query():
            while not stop.is_set():
                page=archive_query.messages('owner','fixture',{'15557654321@s.whatsapp.net'},0,2000000000,100)
                value=json.dumps(page)
                assert len(value)<4194304; counts[0]+=1
        worker=threading.Thread(target=query); worker.start()
        key=directory/'fixture.key'; key.write_text('gsk_'+'x'*48); key.chmod(0o600)
        response=Mock(status=200)
        response.getheader.side_effect=lambda k,default=None:default
        response.read1.side_effect=[b'{"text":"synthetic"}',b'']
        class Connection:
            sock=Mock()
            def __init__(self,*args,**kwargs): self.sent=0
            def putrequest(self,*a): pass
            def putheader(self,*a): pass
            def endheaders(self): pass
            def send(self,data): assert len(data)<=65536; self.sent+=len(data)
            def getresponse(self): return response
            def close(self): pass
        try:
            with AudioSource(Download(),spool).prepare({'drive_id':'fixture','size':10485760,'sha256':digest}) as prepared:
                assert 326<prepared['duration']<329
                with patch('mcp_groq.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch('mcp_groq.http.client.HTTPConnection',Connection):
                    result=GroqClient(key,'y'*48).transcribe(prepared['source'],size=prepared['size'],
                        extension=prepared['extension'],mime_type=prepared['mime_type'],language='auto',deadline=time.monotonic()+65)
                assert result['text']=='synthetic'
            assert not list(spool.iterdir()) and counts[0]>0 and len(cache)==8
            playlist=directory/'bad.m3u'; playlist.write_text('#EXTM3U\nhttp://127.0.0.1:1/private\n')
            try: probe_audio(playlist)
            except ValueError: pass
            else: raise AssertionError('playlist accepted')
        finally:
            stop.set(); worker.join(5)
            pending_store.close(); cursor_store.close()
    cgroup=Path('/sys/fs/cgroup')/next(line.split(':',2)[2].lstrip('/') for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0:'))
    peak=int((cgroup/'memory.peak').read_text())
    print(json.dumps({'whole_cgroup_peak_kib':peak//1024,'under_96mib':peak<100663296,'native_probe':True,'maximum_audio_streamed':True}))
    assert peak<100663296


if __name__=='__main__': main()

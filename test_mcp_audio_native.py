"""Real official ffprobe acceptance; skipped on Windows without /usr/bin/ffprobe."""
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from mcp_audio_source import probe_audio
from mcp_transcription_store import TranscriptionError


@unittest.skipUnless(Path('/usr/bin/ffprobe').is_file() and shutil.which('ffmpeg'),'Linux ffprobe acceptance')
class NativeTests(unittest.TestCase):
    def test_containers_and_playlist(self):
        with tempfile.TemporaryDirectory(dir='.') as name:
            directory=Path(name); source=directory/'source.wav'
            with wave.open(str(source),'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(b'\0'*64000)
            self.assertAlmostEqual(probe_audio(source)['duration'],2,places=2)
            for extension,codec in [('ogg','libopus'),('mp3','libmp3lame'),('flac','flac'),('m4a','aac'),('webm','libopus')]:
                with self.subTest(extension=extension):
                    target=directory/('audio.'+extension)
                    subprocess.run(['/usr/bin/ffmpeg','-v','error','-i',str(source),'-c:a',codec,str(target)],check=True,timeout=10)
                    self.assertLess(abs(probe_audio(target)['duration']-2),.2)
            playlist=directory/'playlist.m3u'; playlist.write_text('#EXTM3U\nhttp://127.0.0.1:1/private\n')
            with self.assertRaises(TranscriptionError): probe_audio(playlist)

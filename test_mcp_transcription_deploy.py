import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from enable_mcp_transcription import enable_transcription
from deploy import RELEASE_FILES
from mcp_transcription_store import LIMITS


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='.'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        for path in ('etc/lylahub-panel','etc/nginx/conf.d','etc/nginx/snippets','var/backups','usr/bin'):
            (self.root/path).mkdir(parents=True,exist_ok=True)
        (self.root/'usr/bin/ffprobe').write_bytes(b'fixture')
        self.config=self.root/'etc/lylahub-panel/mcp.json'
        self.config.write_text(json.dumps({'archive':{'capture_enabled':True},'instance_id':'personal'}))
        self.runner=Mock()
    def prepare(self,**kwargs):
        return enable_transcription(self.root,self.runner,free_plan_verified_at='2026-10-18T15:00:00Z',effective_limits=kwargs.get('limits',LIMITS))
    def test_disabled_prepare_and_limits(self):
        self.prepare()
        value=json.loads(self.config.read_text())
        self.assertFalse(value['transcription']['enabled'])
        self.assertTrue(value['archive']['capture_enabled'])
        self.assertEqual(value['transcription']['limits'],LIMITS)
        self.assertIn('127.0.0.1:8095',(self.root/'etc/nginx/conf.d/lylahub-mcp-groq.conf').read_text())
        with self.assertRaises(ValueError): self.prepare(limits=dict(LIMITS,rpm=21))
    def test_nginx_rollback_preserves_state(self):
        before=self.config.read_bytes()
        state=self.root/'var/backups/usage.fixture'; state.write_bytes(b'unchanged')
        self.runner.side_effect=RuntimeError('nginx rejected')
        with self.assertRaises(RuntimeError): self.prepare()
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(state.read_bytes(),b'unchanged')
        self.assertFalse((self.root/'etc/nginx/conf.d/lylahub-mcp-groq.conf').exists())
    def test_release_contains_modules(self):
        for name in ('mcp_transcription.py','mcp_transcription_store.py','mcp_transcription_cache.py','mcp_audio_source.py','mcp_groq.py'):
            self.assertIn(name,RELEASE_FILES)


if __name__=='__main__': unittest.main()

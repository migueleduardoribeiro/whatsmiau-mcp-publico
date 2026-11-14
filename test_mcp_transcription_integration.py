import threading
import unittest
from unittest.mock import Mock
from mcp_tools import Tools
from mcp_oauth import Principal
from mcp_archive_worker import ArchiveWorker
from mcp_transcription_store import TranscriptionError


class IntegrationTests(unittest.TestCase):
    def test_tool_authorization_and_categories(self):
        transcriber=Mock()
        tools=Tools(Mock(),Mock(),{'instance_id':'fixed'},transcriber=transcriber)
        tools._archive_chats=Mock(return_value={'5511999999999@s.whatsapp.net'})
        spec=next(item for item in tools.list_tools() if item['name']=='transcrever_audio')
        self.assertFalse(spec['annotations']['readOnlyHint'])
        self.assertFalse(spec['annotations']['destructiveHint'])
        self.assertEqual(spec['securitySchemes'][0]['scopes'],['whatsapp.read'])
        transcriber.transcribe.assert_not_called()
        args={'destinatario':'5511999999999','mensagem_id':'one'}
        denied=tools.call('transcrever_audio',args,Principal('owner','other',frozenset({'whatsapp.read'})))
        self.assertEqual(denied['structuredContent']['code'],'access_denied')
        transcriber.transcribe.assert_not_called()
        transcriber.transcribe.side_effect=TranscriptionError('quota_exceeded',12)
        outcome=tools.call('transcrever_audio',args,Principal('owner','fixed',frozenset({'whatsapp.read'})))
        self.assertEqual(outcome['structuredContent'],{'code':'quota_exceeded','retry_after':12})
        transcriber.transcribe.assert_called_once_with('owner','fixed',{'5511999999999@s.whatsapp.net'},'one',language='auto')

    def test_maintenance_failure_preserves_capture(self):
        worker=ArchiveWorker(Mock(),Mock(),Mock(),transfer_slot=threading.BoundedSemaphore(1))
        stop=threading.Event()
        worker.step=Mock(side_effect=lambda:stop.set() or True)
        maintenance=Mock(side_effect=RuntimeError('private'))
        worker.run(stop,maintenance=maintenance)
        worker.step.assert_called_once()
        self.assertIsNone(worker.last_error)


if __name__=='__main__': unittest.main()

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from mcp_oauth import Principal
from mcp_store import Store
from mcp_tools import Tools


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.timeout = False
        self.messages = []
        self.groups = []

    def call(self, path, method='GET', body=None, timeout=None):
        self.calls.append((path, method, body))
        if method == 'POST' and path.endswith('/text'):
            if self.timeout:
                raise TimeoutError('private-secret')
            return {'key': {'id': 'message-1'}, 'status': 'sent'}
        if path.endswith('/contact'):
            return [{'jid': '5511999999999@s.whatsapp.net', 'fullName': 'Maria'},
                    {'jid': '5511888888888@s.whatsapp.net', 'fullName': 'Maria'}]
        if path.endswith('/group/fetchAllGroups'):
            return self.groups
        if path.endswith('syncMessages'):
            return self.messages
        return {'instance': {'state': 'open'}}


class ToolsTests(unittest.TestCase):
    def test_batch_send_retries_without_duplicate_and_stops_on_uncertainty(self):
        args = {'destinatarios': ['5511999999999', '5511888888888', '5511777777777'],
                'texto': 'Oi', 'operacao_id': 'batch-1'}
        original = self.bridge.call
        def send(path, method='GET', body=None, timeout=None):
            if body and body.get('number', '').startswith('551188'):
                self.bridge.calls.append((path, method, body))
                raise TimeoutError()
            return original(path, method, body, timeout)
        self.bridge.call = send
        first = self.call('enviar_texto_lote', args)
        self.assertFalse(first['isError'])
        self.assertEqual([r['status'] for r in first['structuredContent']['resultados']],
                         ['accepted', 'send_uncertain', 'pendente'])
        again = self.call('enviar_texto_lote', args)
        self.assertEqual(again['structuredContent'], first['structuredContent'])
        self.assertEqual(len(self.bridge.calls), 2)
        self.assertTrue(self.call('enviar_texto_lote', {**args, 'texto': 'Alterado'})['isError'])
        self.assertEqual(len(self.bridge.calls), 2)
        read = Principal('profile', 'fixed', frozenset({'whatsapp.read'}))
        self.assertTrue(self.call('enviar_texto_lote', args, read)['isError'])

    def test_batch_send_validates_every_recipient_before_sending(self):
        args = {'destinatarios': ['5511999999999', 'nome'], 'texto': 'Oi', 'operacao_id': 'batch-2'}
        self.assertTrue(self.call('enviar_texto_lote', args)['isError'])
        self.assertEqual(self.bridge.calls, [])

    def test_batch_history_isolated_results_and_read_scope(self):
        class Archive:
            def messages(self, profile, instance, chats, start, end, limit, cursor=None, received_only=False):
                if '5511888888888@s.whatsapp.net' in chats:
                    raise ValueError('invalid_cursor')
                return {'mensagens': [{'id': 'a', 'text': 'hello'}], 'partial': True,
                        'cursor': 'next', 'source': 'drive_archive', 'coverage': {}, 'reason': 'partial'}
        self.tools.archive = Archive()
        args = {'conversas': [{'destinatario': '5511999999999'},
                              {'destinatario': '5511888888888', 'cursor': 'bad'}],
                'inicio': '2026-10-12', 'fim': '2026-10-13'}
        response = self.call('consultar_mensagens_lote', args)
        self.assertFalse(response['isError'])
        rows = response['structuredContent']['resultados']
        self.assertEqual(rows[0]['resultado']['cursor'], 'next')
        self.assertEqual(rows[1]['erro'], 'invalid_cursor')
        spec = next(s for s in self.tools.list_tools() if s['name'] == 'consultar_mensagens_lote')
        self.assertEqual(spec['securitySchemes'][0]['scopes'], ['whatsapp.read'])
        self.assertTrue(self.call('consultar_mensagens_lote', args, Principal('profile', 'other', frozenset({'whatsapp.read'})))['isError'])

    def test_batch_history_rejects_duplicates_and_invalid_fields(self):
        self.tools.archive = object()
        args = {'conversas': [{'destinatario': '5511999999999'}, {'destinatario': '+5511999999999'}],
                'inicio': '2026-10-12', 'fim': '2026-10-13'}
        self.assertEqual(self.call('consultar_mensagens_lote', args)['structuredContent']['code'], 'invalid_arguments')
        args['conversas'] = [{'destinatario': '5511999999999', 'extra': 'x'}]
        self.assertTrue(self.call('consultar_mensagens_lote', args)['isError'])
        self.assertEqual(self.bridge.calls, [])

    def test_archive_image_discovery_explains_how_to_view_content(self):
        class Provider:
            def read_image(self, *args): pass
        class Gateway:
            provider = Provider()
        self.tools.files = Gateway()
        self.tools.archive = object()
        definitions = {item['name']: item for item in self.tools.list_tools()}
        history = definitions['consultar_mensagens']['description']
        image = definitions['consultar_imagem']['description']
        self.assertIn('consultar_imagem', history)
        self.assertIn('has_image', history)
        self.assertIn('antes de concluir', history)
        self.assertIn('bloco de imagem', image)

    def test_edit_text_requires_write_scope_and_is_idempotent(self):
        args = {'destinatario': '5511999999999', 'mensagem_id': 'original',
                'texto': 'Corrigido', 'operacao_id': 'edit-1'}
        read = Principal('profile', 'fixed', frozenset({'whatsapp.read'}))
        self.assertTrue(self.call('editar_texto', args, read)['isError'])
        self.bridge.call = unittest.mock.Mock(return_value={'key': {'id': 'edit-receipt'}, 'status': 'sent'})
        first = self.call('editar_texto', args)
        self.assertFalse(first['isError'])
        self.assertEqual(first['structuredContent'], {'message_id': 'original', 'status': 'accepted'})
        self.assertEqual(self.call('editar_texto', args)['structuredContent'], first['structuredContent'])
        self.bridge.call.assert_called_once_with('/v1/chat/updateMessage/fixed', 'POST', {
            'number': '5511999999999@s.whatsapp.net', 'text': 'Corrigido',
            'key': {'id': 'original', 'remoteJid': '5511999999999@s.whatsapp.net', 'fromMe': True}})
        spec = next(s for s in self.tools.list_tools() if s['name'] == 'editar_texto')
        self.assertFalse(spec['annotations']['readOnlyHint'])
        self.assertEqual(spec['securitySchemes'][0]['scopes'], ['whatsapp.send'])

    def test_edit_uncertain_never_retries(self):
        args = {'destinatario': '5511999999999', 'mensagem_id': 'original',
                'texto': 'Corrigido', 'operacao_id': 'edit-2'}
        self.bridge.call = unittest.mock.Mock(side_effect=TimeoutError())
        self.assertEqual(self.call('editar_texto', args)['structuredContent']['code'], 'send_uncertain')
        self.assertEqual(self.call('editar_texto', args)['structuredContent']['code'], 'send_uncertain')
        self.bridge.call.assert_called_once()

    def test_edit_rejects_invalid_fields_and_operation_id_reuse(self):
        args = {'destinatario': '5511999999999', 'mensagem_id': 'original',
                'texto': 'Corrigido', 'operacao_id': 'edit-3'}
        self.bridge.call = unittest.mock.Mock(return_value={'key': {'id': 'receipt'}})
        for changes in ({'mensagem_id': ''}, {'texto': ''}, {'texto': 'x' * 4097},
                        {'destinatario': 'Maria'}, {'operacao_id': ''}):
            self.assertTrue(self.call('editar_texto', {**args, **changes})['isError'])
        self.bridge.call.assert_not_called()
        self.assertFalse(self.call('editar_texto', args)['isError'])
        self.assertTrue(self.call('editar_texto', {**args, 'texto': 'Outro texto'})['isError'])
        self.bridge.call.assert_called_once()

    def test_send_acknowledgements_preserve_receipts_and_error_details(self):
        from mcp_files import FileGateway
        from test_mcp_files import PNG
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            def fetch_authorized(self, reference, max_bytes): return PNG
            def deliver(self, **kwargs): return {'message_id': 'file-receipt'}
        self.tools = Tools(self.bridge, self.store, {'instance_id': 'fixed'},
            file_gateway=FileGateway({'instance_id': 'fixed'}, self.store, Provider()))
        reference = {'file_id': 'fixture', 'download_url': 'https://files.oaiusercontent.com/fixture',
                     'mime_type': 'image/png', 'file_name': 'fixture.png'}
        for name in ('enviar_texto', 'enviar_documento', 'enviar_imagem'):
            args = {'destinatario': '5511999999999', 'operacao_id': name}
            args.update({'texto': 'Teste'} if name == 'enviar_texto' else {'arquivo': reference})
            with self.subTest(tool=name):
                output = self.call(name, args)
                self.assertFalse(output['isError'])
                self.assertEqual(output['structuredContent']['status'], 'accepted')
                self.assertTrue(output['structuredContent']['message_id'])
                acknowledgement = output['content'][0]['text']
                self.assertTrue(acknowledgement.strip())
                # Human-facing acknowledgement, with the technical receipt kept structured.
                with self.assertRaises(json.JSONDecodeError): json.loads(acknowledgement)
                self.assertEqual(self.call(name, args)['structuredContent'], output['structuredContent'])
        self.bridge.timeout = True
        failure = self.call('enviar_texto', {'destinatario': '5511999999999', 'texto': 'Teste',
                                           'operacao_id': 'uncertain'})
        self.assertTrue(failure['isError'])
        self.assertEqual(failure['structuredContent'], {'code': 'send_uncertain'})
        self.assertEqual(json.loads(failure['content'][0]['text']), failure['structuredContent'])

    def test_phone_history_returns_shared_location(self):
        self.bridge.messages = [{'key': {'id': 'loc', 'remoteJid': '5511999999999@s.whatsapp.net'},
            'messageTimestamp': 1792245600, 'message': {'locationMessage': {
                'degreesLatitude': -23.55, 'degreesLongitude': -46.63}}}]
        output = self.call('consultar_mensagens', {'destinatario': '5511999999999',
            'inicio': '2026-10-17', 'fim': '2026-10-18', 'referencia_id': 'anchor',
            'referencia_enviada_por_mim': False})
        self.assertFalse(output['isError'])
        location = output['structuredContent']['mensagens'][0]['location']
        self.assertEqual(location['latitude'], -23.55)
        self.assertEqual(location['longitude'], -46.63)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.bridge = FakeBridge()
        self.tools = Tools(self.bridge, self.store, {'instance_id': 'fixed'})
        self.principal = Principal('profile', 'fixed', frozenset({'whatsapp.read', 'whatsapp.send'}))

    def call(self, name, args=None, principal=None):
        return self.tools.call(name, args or {}, principal or self.principal)

    def send(self, **changes):
        args = {'destinatario': '5511999999999', 'texto': 'Bom dia', 'operacao_id': 'op-1'}
        args.update(changes)
        return self.call('enviar_texto', args)

    def history(self, **changes):
        args = {'destinatario': '5511999999999', 'inicio': '2026-10-12', 'fim': '2026-10-13',
                'referencia_id': 'known-message', 'referencia_enviada_por_mim': False}
        args.update(changes)
        return self.call('consultar_mensagens', args)

    def test_send_accepted_is_not_delivered_and_same_receipt_does_not_resend(self):
        first = self.send()['structuredContent']
        second = self.send()['structuredContent']
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(len(self.bridge.calls), 1)

    def test_send_timeout_is_uncertain_and_not_retried(self):
        self.bridge.timeout = True
        result = self.send()
        self.assertTrue(result['isError'])
        self.assertEqual(result['structuredContent']['code'], 'send_uncertain')
        self.send()
        self.assertEqual(len(self.bridge.calls), 1)
        self.assertNotIn('private-secret', json.dumps(result))

    def test_input_validation_before_dispatch(self):
        for changes in ({'texto': ''}, {'texto': 'a' * 4097}, {'destinatario': 'Maria'},
                        {'destinatario': '123@g.us'}, {'instance_id': 'other'},
                        {'texto': 1}, {'operacao_id': ''}):
            with self.subTest(changes=changes):
                self.assertTrue(self.send(**changes)['isError'])
        self.assertEqual(self.bridge.calls, [])

    def test_read_grant_cannot_send_and_wrong_instance_cannot_read(self):
        read = Principal('profile', 'fixed', frozenset({'whatsapp.read'}))
        self.assertTrue(self.call('enviar_texto', {'destinatario': '5511999999999', 'texto': 'x',
                                                'operacao_id': 'op'}, read)['isError'])
        wrong = Principal('profile', 'other', self.principal.scopes)
        self.assertTrue(self.call('consultar_conexao', {}, wrong)['isError'])
        self.assertFalse(self.bridge.calls)

    def test_ambiguous_contacts_are_candidates_not_auto_selected(self):
        result = self.call('buscar_contatos', {'consulta': 'Maria'})['structuredContent']
        self.assertEqual(len(result['contatos']), 2)
        self.assertTrue(result['ambiguous'])

    def test_contact_search_includes_joined_groups_by_subject(self):
        jid = '120363012345678901-123456@g.us'
        self.bridge.groups = [{'id': jid, 'subject': 'Equipe Produto'}]
        result = self.call('buscar_contatos', {'consulta': 'equipe'})['structuredContent']
        self.assertEqual(result['contatos'], [{'destinatario': jid, 'nome': 'Equipe Produto'}])
        self.assertFalse(result['ambiguous'])

    def test_text_send_accepts_explicit_group_jid(self):
        jid = '120363012345678901-123456@g.us'
        result = self.send(destinatario=jid)
        self.assertFalse(result['isError'])
        self.assertEqual(self.bridge.calls[-1][2], {'number': jid, 'text': 'Bom dia'})

    def test_audio_transcription_accepts_group_jid(self):
        jid = '120363012345678901-123456@g.us'
        class Transcriber:
            calls = []
            def transcribe(self, profile, instance, chats, message_id, language='auto'):
                self.calls.append((profile, instance, chats, message_id, language))
                return {'status': 'completed', 'text': 'audio lido'}
        transcriber = Transcriber()
        self.tools = Tools(self.bridge, self.store, {'instance_id': 'fixed'},
                           archive=object(), transcriber=transcriber)
        output = self.call('transcrever_audio', {'destinatario': jid, 'mensagem_id': 'audio-1'})
        self.assertFalse(output['isError'])
        self.assertEqual(transcriber.calls[0][2], {jid})

    def test_contact_limit_rejects_bool_and_overflow(self):
        for limit in (True, 0, 101):
            self.assertTrue(self.call('buscar_contatos', {'consulta': 'Maria', 'limite': limit})['isError'])

    def test_no_anchor_returns_reference_required(self):
        result = self.history(referencia_id=None)
        self.assertEqual(result['structuredContent']['code'], 'reference_required')
        self.assertFalse(self.bridge.calls)

    def test_time_zone_and_exclusive_end_and_no_media_secrets(self):
        self.bridge.messages = [
            {'key': {'id': 'inside', 'remoteJid': '5511999999999@s.whatsapp.net'},
             'messageTimestamp': 1791774000, 'messageType': 'imageMessage',
             'message': {'imageMessage': {'caption': 'photo', 'mediaKey': 'secret', 'url': 'private-url'}}},
            {'key': {'id': 'outside', 'remoteJid': '5511999999999@s.whatsapp.net'},
             'messageTimestamp': 1791860400, 'message': {'conversation': 'outside'}}]
        data = self.history()['structuredContent']
        self.assertEqual([m['id'] for m in data['mensagens']], ['inside'])
        self.assertNotIn('secret', json.dumps(data))
        self.assertNotIn('private-url', json.dumps(data))
        self.assertTrue(data['partial'])

    def test_cursor_is_bound_to_recipient_and_period(self):
        self.bridge.messages = [{'key': {'id': 'inside', 'remoteJid': '5511999999999@s.whatsapp.net'},
                                 'messageTimestamp': 1791774060, 'message': {'conversation': 'hi'}}]
        cursor = self.history()['structuredContent']['cursor']
        self.assertTrue(cursor)
        result = self.history(cursor=cursor, destinatario='5511888888888')
        self.assertTrue(result['isError'])

    def test_partial_timeout_does_not_report_empty_history(self):
        def fail(*args, **kwargs):
            raise TimeoutError()
        self.bridge.call = fail
        result = self.history()
        self.assertEqual(result['structuredContent']['code'], 'history_timeout')
        self.assertNotIn('mensagens', result['structuredContent'])

    def test_other_conversation_is_not_returned(self):
        self.bridge.messages = [{'key': {'id': 'wrong', 'remoteJid': '5511888888888@s.whatsapp.net'},
                                 'messageTimestamp': 1791774000, 'message': {'conversation': 'secret'}}]
        data = self.history()['structuredContent']
        self.assertEqual(data['mensagens'], [])
        self.assertTrue(data['partial'])

    def test_overlarge_response_is_rejected(self):
        self.bridge.messages = [{'unexpected': 'x' * (2 * 1024 * 1024 + 1)}]
        self.assertTrue(self.history()['isError'])

    def test_file_tools_only_advertised_with_real_provider(self):
        self.assertNotIn('enviar_documento', {tool['name'] for tool in self.tools.list_tools()})
        from mcp_files import FileGateway
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            def fetch_authorized(self, reference, max_bytes):
                return b'print(1)'
            def deliver(self, **kwargs):
                self.sent = kwargs
                return {'message_id': 'file-receipt'}
        provider = Provider()
        self.tools.files = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        definitions = {tool['name']: tool for tool in self.tools.list_tools()}
        self.assertEqual(definitions['enviar_documento']['_meta']['openai/fileParams'], ['arquivo'])
        output = self.call('enviar_documento', {'destinatario': '5511999999999', 'operacao_id': 'file-op',
                           'arquivo': {'file_id': 'file-fixture', 'download_url': 'https://files.oaiusercontent.com/fixture',
                                       'file_name': 'example.py', 'mime_type': 'text/x-python'}})
        self.assertFalse(output['isError'])
        self.assertEqual(provider.sent['filename'], 'example.py')
        self.assertEqual(provider.sent['data'], b'print(1)')

    def test_file_transfer_has_its_own_memory_slot(self):
        from mcp_files import FileGateway
        self.tools.files = FileGateway({'instance_id': 'fixed'}, self.store, object())
        self.tools.file_slots.acquire()
        try:
            output = self.call('enviar_documento', {'destinatario': '5511999999999', 'operacao_id': 'file-op', 'arquivo': {}})
            self.assertEqual(output['structuredContent']['code'], 'busy')
        finally:
            self.tools.file_slots.release()

    def test_history_gate_rejects_parallel_query_without_upstream_dispatch(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        original = self.bridge.call
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        self.bridge.call = blocked
        worker = threading.Thread(target=self.history)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.history()['structuredContent'], {'code': 'busy'})
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(self.history()['isError'])

    def test_history_accepts_only_authenticated_phone_to_lid_mapping(self):
        jid, lid = '5511999999999@s.whatsapp.net', '123456789012345@lid'
        self.bridge.messages = [{'key': {'id': 'mapped-id', 'remoteJid': lid},
                                 'messageTimestamp': 1791781200, 'message': {'conversation': 'mapped'}}]
        original = self.bridge.call
        def mapped(path, method='GET', body=None, **kwargs):
            if path.startswith('/v1/chat/whatsappNumbers/'):
                return [{'exists': True, 'jid': jid, 'lid': lid, 'number': '5511999999999'}]
            return original(path, method, body, **kwargs)
        self.bridge.call = mapped
        response = self.history()['structuredContent']
        self.assertEqual([m['id'] for m in response['mensagens']], ['mapped-id'])
        self.bridge.messages[0]['key']['remoteJid'] = '999999999999999@lid'
        self.assertEqual(self.history()['structuredContent'], {'code': 'history_identity_unresolved'})

    def test_history_upstream_timeout_is_explicit_and_redacted(self):
        from server import APIError
        def timeout(*args, **kwargs):
            raise APIError(504, 'private-upstream-details')
        self.bridge.call = timeout
        response = self.history()
        self.assertEqual(response['structuredContent'], {'code': 'history_timeout'})
        self.assertTrue(response['isError'])
        self.assertNotIn('private-upstream-details', json.dumps(response))
        self.assertNotIn('mensagens', response['structuredContent'])

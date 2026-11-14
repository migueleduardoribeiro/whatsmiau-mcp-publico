import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp_files import FileGateway
from mcp_oauth import Principal
from mcp_store import Store
from test_mcp_files import PNG


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.principal = Principal('owner', 'fixed', frozenset({'whatsapp.read'}))

    def test_image_description_reaches_text_only_clients_after_authorization(self):
        from mcp_vision import ImageAnalyzer
        client = Mock()
        client.describe.return_value = 'Um quadro com uma tabela-verdade.'
        analyzer = ImageAnalyzer(client, self.store)
        archive = Mock()
        archive.image.return_value = {'data': PNG, 'mime_type': 'image/png'}
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, archive=archive,
                              image_analyzer=analyzer)
        output = gateway.read_image(self.principal, '5511999999999', 'image-1')
        self.assertEqual(output['structuredContent']['analysis']['description'], client.describe.return_value)
        self.assertIn('tabela-verdade', output['content'][0]['text'])
        visual = next(c for c in output['content'] if c['type'] == 'image')
        self.assertEqual(base64.b64decode(visual['data']), PNG)
        client.describe.assert_called_once()
        self.assertEqual(client.describe.call_args.args, (PNG, 'image/png'))
        gateway.read_image(self.principal, '5511999999999', 'image-1')
        self.assertEqual(client.describe.call_count, 1)
        denied = Principal('owner', 'other', frozenset({'whatsapp.read'}))
        with self.assertRaises(ValueError): gateway.read_image(denied, '5511999999999', 'image-1')
        self.assertEqual(client.describe.call_count, 1)

    def test_provider_failure_preserves_image_and_reports_analysis_error(self):
        from mcp_vision import ImageAnalyzer, VisionError
        client = Mock()
        client.describe.side_effect = VisionError('image_analysis_unavailable')
        provider = Mock()
        provider.read_image.return_value = {'data': PNG, 'mime_type': 'image/png'}
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider,
                              image_analyzer=ImageAnalyzer(client, self.store))
        output = gateway.read_image(self.principal, '5511999999999', 'image-1')
        self.assertFalse(output['isError'])
        self.assertEqual(output['structuredContent']['analysis'],
                         {'status': 'unavailable', 'code': 'image_analysis_unavailable'})
        self.assertIn('image', [c['type'] for c in output['content']])

    def test_durable_quota_and_cache_ttl_do_not_store_private_descriptions(self):
        from mcp_vision import ImageAnalyzer, VisionError
        now = [100000.0]
        client = Mock(); client.describe.return_value = 'private-description'
        analyzer = ImageAnalyzer(client, self.store, clock=lambda: now[0])
        analyzer.analyze(PNG, 'image/png', 'owner')
        analyzer.analyze(PNG, 'image/png', 'other-owner')
        with self.assertRaisesRegex(VisionError, 'image_analysis_quota_exceeded'):
            ImageAnalyzer(client, self.store, clock=lambda: now[0]).analyze(PNG, 'image/png', 'third')
        self.assertNotIn(b'private-description', (Path(self.temp.name) / 'state.db').read_bytes())
        now[0] += 901
        analyzer.analyze(PNG, 'image/png', 'owner')
        self.assertEqual(client.describe.call_count, 3)

    def test_vision_client_uses_fixed_endpoint_and_only_image_payload(self):
        from mcp_vision import GroqVisionClient, MODEL
        key = Path(self.temp.name) / 'groq.key'
        key.write_text('gsk_' + 'a' * 48); key.chmod(0o600)
        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': 'Quadro verde'}}]}).encode()
        reply = Mock(status=200)
        reply.getheader.side_effect = lambda k, default=None: {'Content-Length': str(len(raw))}.get(k, default)
        reply.isclosed.return_value = False
        reply.read1.side_effect = [raw, b'']
        connection = Mock(); connection.getresponse.return_value = reply
        with patch('mcp_vision.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
             patch('mcp_vision.http.client.HTTPConnection', return_value=connection):
            result = GroqVisionClient(key, 'x' * 48).describe(PNG, 'image/png', deadline=time.monotonic()+30)
        self.assertEqual(result, 'Quadro verde')
        self.assertEqual(connection.putrequest.call_args.args, ('POST', '/openai/v1/chat/completions'))
        payload = json.loads(b''.join(c.args[0] for c in connection.send.call_args_list))
        self.assertEqual(payload['model'], MODEL)
        self.assertEqual(payload['messages'][1]['content'][1]['image_url']['url'],
                         'data:image/png;base64,' + base64.b64encode(PNG).decode())
        self.assertNotIn('tools', payload)
        self.assertNotIn('5511999999999', json.dumps(payload))
        self.assertNotIn('owner', json.dumps(payload))

    def test_proxy_allows_only_explicit_vision_post_route_when_enabled(self):
        from mcp_transcription_deploy import groq_proxy_config
        self.assertNotIn('/chat/completions', groq_proxy_config())
        text = groq_proxy_config(vision=True)
        self.assertEqual(text.count('location = /openai/v1/chat/completions {'), 1)
        self.assertEqual(text.count('proxy_ssl_verify on;'), 2)
        self.assertEqual(text.count('limit_except POST { deny all; }'), 2)
        self.assertIn('location / { return 404; }', text)

    def test_enable_requires_explicit_authorization_and_rolls_back_on_failure(self):
        from enable_mcp_vision import enable_vision
        root = Path(self.temp.name)
        config = root/'etc/lylahub-panel/mcp.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'transcription': {'enabled': True, 'key_path': '/protected',
            'proxy_token': 'x'*48}}))
        proxy = root/'etc/nginx/conf.d/lylahub-mcp-groq.conf'
        proxy.parent.mkdir(parents=True); proxy.write_text('previous-conf')
        before = config.read_bytes()
        with self.assertRaises(ValueError): enable_vision(root, Mock(), authorized_at='', free_plan_verified_at='')
        with self.assertRaises(RuntimeError):
            enable_vision(root, Mock(side_effect=RuntimeError('nginx failed')),
                authorized_at='2026-10-27T00:00:00Z', free_plan_verified_at='2026-10-27T00:00:00Z')
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(proxy.read_text(), 'previous-conf')
        outcome = enable_vision(root, Mock(), authorized_at='2026-10-27T00:00:00Z',
                                free_plan_verified_at='2026-10-27T00:00:00Z')
        self.assertTrue(outcome['enabled'])
        self.assertTrue(json.loads(config.read_text())['vision']['enabled'])
        self.assertIn('/chat/completions', proxy.read_text())

    def test_tool_schema_accepts_text_description_and_reports_external_inference(self):
        import jsonschema
        from mcp_tools import Tools, output_schema
        from mcp_vision import ImageAnalyzer
        client = Mock(); client.describe.return_value = 'Quadro de lógica.'
        provider = Mock(); provider.read_image.return_value = {'data': PNG, 'mime_type': 'image/png'}
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider,
                              image_analyzer=ImageAnalyzer(client, self.store))
        tools = Tools(Mock(), self.store, {'instance_id': 'fixed'}, file_gateway=gateway)
        output = tools.call('consultar_imagem', {'destinatario': '5511999999999', 'mensagem_id': 'one'}, self.principal)
        jsonschema.validate(output['structuredContent'], output_schema('consultar_imagem'))
        spec = next(s for s in tools.list_tools() if s['name'] == 'consultar_imagem')
        self.assertIn('Groq', spec['description'])
        self.assertFalse(spec['annotations']['readOnlyHint'])
        self.assertEqual(spec['securitySchemes'][0]['scopes'], ['whatsapp.read'])

    def test_vision_rejects_redirect_private_dns_and_incomplete_provider_output(self):
        from mcp_vision import GroqVisionClient, VisionError
        key = Path(self.temp.name)/'groq.key'
        key.write_text('gsk_'+'a'*48); key.chmod(0o600)
        client = GroqVisionClient(key, 'x'*48)
        with patch('mcp_vision.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
             patch('mcp_vision.http.client.HTTPConnection') as factory:
            with self.assertRaisesRegex(VisionError, 'image_analysis_unavailable'):
                client.describe(PNG, 'image/png', deadline=time.monotonic()+30)
            factory.assert_not_called()
        for status, body, expected in (
            (302, {}, 'image_analysis_uncertain'),
            (429, {'secret': 'private'}, 'image_analysis_provider_rate_limited'),
            (400, {'secret': 'private'}, 'image_analysis_rejected'),
            (200, {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}, 'image_analysis_incomplete'),
            (200, {'choices': []}, 'image_analysis_uncertain'),
        ):
            with self.subTest(status=status, body=body):
                raw = json.dumps(body).encode()
                reply = Mock(status=status)
                reply.getheader.side_effect = lambda k, default=None: {'Content-Length': str(len(raw)), 'Retry-After':'12'}.get(k, default)
                reply.isclosed.return_value = False
                reply.read1.side_effect = [raw, b'']
                connection = Mock(); connection.getresponse.return_value = reply
                with patch('mcp_vision.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
                     patch('mcp_vision.http.client.HTTPConnection', return_value=connection) as factory:
                    with self.assertRaisesRegex(VisionError, expected):
                        client.describe(PNG, 'image/png', deadline=time.monotonic()+30)
                    self.assertEqual(factory.call_count, 1)
                    connection.close.assert_called_once()

    def test_rate_limit_retry_is_respected_without_new_provider_request(self):
        from mcp_vision import ImageAnalyzer, VisionError
        client = Mock(); client.describe.side_effect = VisionError('image_analysis_provider_rate_limited', 120)
        analyzer = ImageAnalyzer(client, self.store, clock=lambda: 100000)
        for scope in ('one', 'two'):
            with self.assertRaisesRegex(VisionError, 'image_analysis_provider_rate_limited'):
                analyzer.analyze(PNG, 'image/png', scope)
        self.assertEqual(client.describe.call_count, 1)

    def test_server_does_not_initialize_or_infer_without_opt_in(self):
        from server import configure_mcp
        config = {'issuer': 'https://fixture.example', 'instance_id': 'fixed', 'allowed_user': 'owner',
                  'clients': {}, 'state_path': str(Path(self.temp.name)/'config.db')}
        with patch('mcp_vision.GroqVisionClient') as client:
            app = configure_mcp(config, Mock()); app.oauth.store.close()
            client.assert_not_called()
            for vision in ({'enabled': 'yes'}, {'enabled': True}):
                with self.assertRaises(ValueError): configure_mcp({**config, 'vision': vision}, Mock())
            client.assert_not_called()

    def test_daily_limit_survives_restart_without_persisting_photo_content(self):
        from mcp_vision import ImageAnalyzer, VisionError
        now = [100000.0]
        client = Mock(); client.describe.return_value = 'Descrição privada.'
        for index in range(30):
            ImageAnalyzer(client, self.store, clock=lambda: now[0]).analyze(PNG, 'image/png', 'owner')
            now[0] += 61
        with self.assertRaisesRegex(VisionError, 'image_analysis_quota_exceeded'):
            ImageAnalyzer(client, self.store, clock=lambda: now[0]).analyze(PNG, 'image/png', 'owner')
        self.assertEqual(client.describe.call_count, 30)
        now[0] += 86400
        ImageAnalyzer(client, self.store, clock=lambda: now[0]).analyze(PNG, 'image/png', 'owner')
        self.assertEqual(client.describe.call_count, 31)

    def test_stalled_dns_obeys_deadline_and_does_not_spawn_more_resolvers(self):
        from mcp_vision import GroqVisionClient, VisionError
        key = Path(self.temp.name)/'groq.key'
        key.write_text('gsk_'+'a'*48); key.chmod(0o600)
        client = GroqVisionClient(key, 'x'*48)
        release = threading.Event()
        def resolve(*args):
            release.wait(2)
            return [(2, 1, 6, '', ('8.8.8.8', 443))]
        try:
            with patch('mcp_vision.socket.getaddrinfo', side_effect=resolve) as dns:
                start = time.monotonic()
                for _ in range(2):
                    with self.assertRaises(VisionError):
                        client.describe(PNG, 'image/png', deadline=time.monotonic()+.05)
                self.assertLess(time.monotonic()-start, .5)
                self.assertEqual(dns.call_count, 1)
        finally:
            release.set()


if __name__ == '__main__': unittest.main()

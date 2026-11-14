import base64
import tempfile
import unittest
from pathlib import Path
from mcp_files import FileGateway, FileError, file_input_schema, validate_file, validate_file_reference
from mcp_oauth import Principal
from mcp_store import Store

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=')


class FilesTests(unittest.TestCase):
    def test_opaque_file_ids_reach_delivery_and_retry_without_duplicate(self):
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            calls = 0
            def fetch_authorized(self, reference, max_bytes):
                return b'%PDF-1.7\nopaque fixture\n%%EOF'
            def deliver(self, **kwargs):
                self.calls += 1
                self.sent = kwargs
                return {'message_id': 'receipt-' + str(self.calls)}
        provider = Provider()
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        # Opaque strings per the official file-input contract, not assumed ID formats.
        for index, identifier in enumerate(('file:generated/report.pdf', 'token/part+value=',
                                             'ref with spaces', 'arquivo-é', 'x' * 300)):
            with self.subTest(identifier=identifier):
                reference = {'file_id': identifier,
                    'download_url': 'https://files.oaiusercontent.com/signed',
                    'file_name': 'relatorio.pdf', 'mime_type': 'application/pdf'}
                receipt = gateway.send_file(self.principal, '5511999999999', reference,
                    'document', 'teste de automação', 'opaque-' + str(index))
                self.assertEqual(receipt['status'], 'accepted')
                retry = gateway.send_file(self.principal, '5511999999999', reference,
                    'document', 'teste de automação', 'opaque-' + str(index))
                self.assertEqual(retry, receipt)
                self.assertEqual(provider.calls, index + 1)
                self.assertEqual(provider.sent['filename'], 'relatorio.pdf')
                self.assertEqual(provider.sent['data'], b'%PDF-1.7\nopaque fixture\n%%EOF')

    def test_opaque_ids_do_not_bypass_download_url_checks(self):
        for url in ('sandbox:/mnt/data/report.pdf', 'file:///etc/passwd',
                    'http://files.oaiusercontent.com/a', 'https://evil.example/a',
                    'https://127.0.0.1/a'):
            with self.subTest(url=url), self.assertRaises(FileError):
                validate_file_reference({'file_id': 'file:generated/report.pdf', 'download_url': url},
                    {'files.oaiusercontent.com'})

    def test_empty_control_or_oversize_ids_are_rejected(self):
        for identifier in ('', '  ', 'id\nline', 'id\x00', 'x' * 4097):
            with self.subTest(identifier=identifier), self.assertRaises(FileError):
                validate_file_reference({'file_id': identifier,
                    'download_url': 'https://files.oaiusercontent.com/signed'}, {'files.oaiusercontent.com'})

    def test_bad_file_reference_explains_failure_without_sending_or_disclosing_url(self):
        from mcp_tools import Tools
        from unittest.mock import Mock
        provider = Mock(allowed_hosts={'files.oaiusercontent.com'})
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        tools = Tools(None, self.store, {'instance_id': 'fixed'}, file_gateway=gateway)
        cases = [('/mnt/data/aminoacidos.pdf', 'file_reference_not_resolved'),
                 ({'file_id': 'file-test'}, 'missing_download_url'),
                 ({'file_id': 'file-test', 'download_url': 'sandbox:/mnt/data/aminoacidos.pdf'},
                  'local_file_reference'),
                 ({'file_id': 'file-test', 'download_url': 'https://other.invalid/file?secret=private'},
                  'download_host_not_allowed'),
                 ({'file_id': '', 'download_url': 'https://files.oaiusercontent.com/file'},
                  'missing_file_id')]
        for reference, reason in cases:
            with self.subTest(reason=reason):
                output = tools.call('enviar_documento', {'destinatario': '5511999999999',
                    'arquivo': reference, 'operacao_id': 'failed-reference'}, self.principal)
                self.assertTrue(output['isError'])
                detail = output['structuredContent']
                self.assertEqual(detail['code'], 'invalid_file_reference')
                self.assertEqual(detail.get('reason'), reason)
                self.assertTrue(detail['retry_safe'])
                self.assertTrue(detail['message'])
                self.assertNotIn('secret=private', str(output))
                if reason == 'download_host_not_allowed':
                    self.assertEqual(detail.get('download_host'), 'other.invalid')
                    self.assertNotIn('/file?', str(output))
        provider.fetch_authorized.assert_not_called()
        provider.deliver.assert_not_called()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM sends').fetchone()[0], 0)

    def test_observer_failure_keeps_accepted_receipt(self):
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            calls = 0
            def fetch_authorized(self, reference, max_bytes): return b'opaque zip bytes'
            def deliver(self, **kwargs): self.calls += 1; return {'message_id': 'accepted-file'}
        provider = Provider()
        def failed_observer(event):
            self.assertEqual(event['data'], b'opaque zip bytes')
            raise RuntimeError('archive unavailable')
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider, send_observer=failed_observer)
        reference = {'download_url': 'https://files.oaiusercontent.com/file', 'file_id': 'fixture-file'}
        receipt = gateway.send_file(self.principal, '5511999999999', reference, 'document', '', 'one')
        self.assertEqual(receipt['status'], 'accepted')
        self.assertEqual(gateway.send_file(self.principal, '5511999999999', reference, 'document', '', 'one'), receipt)
        self.assertEqual(provider.calls, 1)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.principal = Principal('owner', 'fixed', frozenset({'whatsapp.read', 'whatsapp.send'}))

    def test_contract_declares_all_four_properties(self):
        spec = file_input_schema()
        self.assertEqual(set(spec['properties']), {'download_url', 'file_id', 'mime_type', 'file_name'})
        self.assertEqual(set(spec['required']), {'download_url', 'file_id'})

    def test_png_and_pdf_and_mime_mismatch(self):
        self.assertEqual(validate_file(PNG, 'image/png', 'image'), 'image/png')
        self.assertEqual(validate_file(PNG, None, 'image'), 'image/png')
        self.assertEqual(validate_file(b'%PDF-1.7\nfixture\n%%EOF', 'application/pdf', 'document'), 'application/pdf')
        for data, mime, kind in [(PNG, 'image/jpeg', 'image'), (b'not image', 'image/png', 'image'),
                                 (b'%PDF-fake', 'image/png', 'image')]:
            with self.subTest(mime=mime), self.assertRaises(FileError):
                validate_file(data, mime, kind)

    def test_any_document_type_is_allowed_as_opaque_bytes(self):
        cases = [(b'PK\x03\x04opaque archive', 'application/zip'),
                 (b'print("example")\n', 'text/x-python'),
                 (b'PK\x03\x04opaque presentation', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
                 (b'PK\x03\x04opaque spreadsheet', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                 (b'\x00\x01\xffarbitrary', 'application/octet-stream'), (b'<html>file</html>', 'text/html')]
        for data, mime in cases:
            with self.subTest(mime=mime):
                self.assertEqual(validate_file(data, mime, 'document'), mime)
        self.assertEqual(validate_file(b'opaque', None, 'document'), 'application/octet-stream')
        self.assertEqual(validate_file(b'', 'text/x-python', 'document'), 'text/x-python')

    def test_document_mime_cannot_inject_headers(self):
        with self.assertRaises(FileError):
            validate_file(b'opaque', 'text/plain\r\nInjected: header', 'document')

    def test_code_file_preserves_bytes_and_name_and_is_never_executed(self):
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            def fetch_authorized(self, reference, max_bytes):
                return b'raise RuntimeError("must not run")\n'
            def deliver(self, **kwargs):
                self.sent = kwargs
                return {'message_id': 'receipt'}
        provider = Provider()
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        ref = {'download_url': 'https://files.oaiusercontent.com/signed', 'file_id': 'file-code',
               'file_name': 'script.py', 'mime_type': 'text/x-python'}
        gateway.send_file(self.principal, '5511999999999', ref, 'document', '', 'op-code')
        self.assertEqual(provider.sent['filename'], 'script.py')
        self.assertEqual(provider.sent['data'], b'raise RuntimeError("must not run")\n')
        self.assertEqual(provider.sent['kind'], 'document')

    def test_size_limits(self):
        for data, mime, kind in [(PNG + b'x' * (5 * 1024 * 1024), 'image/png', 'image'),
                                 (b'%PDF-' + b'x' * (10 * 1024 * 1024), 'application/pdf', 'document')]:
            with self.assertRaises(FileError):
                validate_file(data, mime, kind)

    def test_untrusted_urls_and_local_paths_are_rejected(self):
        for url in ('sandbox:/mnt/data/image.png', 'file:///etc/passwd', 'http://example.com/a',
                    'https://127.0.0.1/a', 'https://localhost/a', 'https://192.0.2.10/a',
                    'https://evil.example/a', 'https://files.oaiusercontent.com@evil.example/a',
                    'https://files.oaiusercontent.com:444/a'):
            with self.subTest(url=url), self.assertRaises(FileError):
                validate_file_reference({'download_url': url, 'file_id': 'file-fixture'},
                                        {'files.oaiusercontent.com'})

    def test_provider_is_required_and_unsupported_capability_is_explicit(self):
        gateway = FileGateway({'instance_id': 'fixed'}, self.store)
        with self.assertRaisesRegex(FileError, 'file_transport_unavailable'):
            gateway.send_file(self.principal, '5511999999999', {}, 'image', '', 'op')
        with self.assertRaisesRegex(FileError, 'image_download_unavailable'):
            gateway.read_image(self.principal, '5511999999999', 'message-id')

    def test_send_image_and_document_kinds_and_receipts(self):
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            calls = []
            data = PNG
            def fetch_authorized(self, reference, max_bytes):
                return self.data
            def deliver(self, **kwargs):
                self.calls.append(kwargs)
                return {'message_id': 'receipt', 'status': 'accepted'}
        provider = Provider()
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        ref = {'download_url': 'https://files.oaiusercontent.com/signed', 'file_id': 'file-fixture', 'mime_type': 'image/png'}
        gateway.send_file(self.principal, '5511999999999', ref, 'image', 'caption', 'op')
        self.assertEqual(provider.calls[0]['kind'], 'image')
        gateway.send_file(self.principal, '5511999999999', ref, 'image', 'caption', 'op')
        self.assertEqual(len(provider.calls), 1)
        provider.data = b'%PDF-1.7\nfixture\n%%EOF'
        ref = dict(ref, file_id='file-pdf', mime_type='application/pdf')
        gateway.send_file(self.principal, '5511999999999', ref, 'document', '', 'op-2')
        self.assertEqual(provider.calls[-1]['kind'], 'document')
        group = '120363012345678901-123456@g.us'
        gateway.send_file(self.principal, group, ref, 'document', '', 'op-3')
        self.assertEqual(provider.calls[-1]['recipient'], group)

    def test_image_result_is_visual_content_and_not_a_ciphertext_link(self):
        class Provider:
            def read_image(self, instance, recipient, message_id):
                return {'data': PNG, 'mime_type': 'image/png', 'caption': 'foto', 'timestamp': '2026-10-16T12:00:00Z'}
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, Provider())
        result = gateway.read_image(self.principal, '5511999999999', 'message-id')
        visual = next(x for x in result['content'] if x['type'] == 'image')
        self.assertEqual(base64.b64decode(visual['data']), PNG)
        self.assertEqual(visual['mimeType'], 'image/png')

    def test_invalid_upstream_image_and_permissions(self):
        class Provider:
            def read_image(self, *args):
                return {'data': b'ciphertext', 'mime_type': 'image/png'}
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, Provider())
        with self.assertRaises(FileError):
            gateway.read_image(self.principal, '5511999999999', 'message-id')
        with self.assertRaises(FileError):
            gateway.read_image(Principal('p', 'other', self.principal.scopes), '5511999999999', 'message-id')

    def test_truncated_images_are_rejected(self):
        for data in (PNG[:33], b'\xff\xd8\xff\xff\xd9', PNG[:-12]):
            with self.subTest(data=data), self.assertRaises(FileError):
                validate_file(data, None, 'image')

    def test_accepted_receipt_survives_expired_download_url(self):
        class Provider:
            allowed_hosts = {'files.oaiusercontent.com'}
            def fetch_authorized(self, *args):
                if getattr(self, 'expired', False):
                    raise FileError('media_download_failed')
                return b'file'
            def deliver(self, **kwargs):
                return {'message_id': 'accepted-id'}
        provider = Provider()
        gateway = FileGateway({'instance_id': 'fixed'}, self.store, provider)
        ref = {'download_url': 'https://files.oaiusercontent.com/signed', 'file_id': 'file-stable'}
        receipt = gateway.send_file(self.principal, '5511999999999', ref, 'document', '', 'stable-op')
        provider.expired = True
        self.assertEqual(gateway.send_file(self.principal, '5511999999999', ref, 'document', '', 'stable-op'), receipt)
        from mcp_store import StateError
        with self.assertRaises(StateError):
            gateway.send_file(self.principal, '5511999999999', dict(ref, file_id='different'), 'document', '', 'stable-op')

    def test_complete_jpeg_and_corrupt_png_crc(self):
        jpeg = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5/ooooA//2Q==')
        self.assertEqual(validate_file(jpeg, 'image/jpeg', 'image'), 'image/jpeg')
        corrupted = bytearray(PNG)
        corrupted[40] ^= 1
        with self.assertRaises(FileError):
            validate_file(bytes(corrupted), None, 'image')

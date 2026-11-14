import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from mcp_groq import GroqClient
from mcp_transcription_store import TranscriptionError
from mcp_transcription_deploy import groq_proxy_config


class NoBulkRead(io.BytesIO):
    def read(self,size=-1):
        if not 0 <= size <= 65536: raise AssertionError('bulk read')
        return super().read(size)


class GroqTests(unittest.TestCase):
    def test_real_http_connection_close_lifecycle(self):
        import http.client
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200); self.send_header('Connection','close')
                self.send_header('Content-Length','16'); self.end_headers()
                self.wfile.write(b'{"text":"hello"}')
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.handle_request); thread.start()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            with patch('mcp_groq.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch('mcp_groq.http.client.HTTPConnection',return_value=connection):
                # Connect first: the patched DNS result belongs to the remote pin, not localhost.
                import socket
                connection.sock=socket.socket(); connection.sock.settimeout(3)
                connection.sock.connect(('127.0.0.1',server.server_port))
                value=GroqClient(self.key,'x'*48).transcribe(io.BytesIO(b'a'),size=1,extension='ogg',
                    mime_type='audio/ogg',language='auto',deadline=time.monotonic()+10)
            self.assertEqual(value['text'],'hello')
        finally:
            connection.close(); server.server_close(); thread.join(5)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='.'); self.addCleanup(self.temp.cleanup)
        self.key=Path(self.temp.name)/'groq.key'; self.key.write_text('gsk_'+'a'*48); self.key.chmod(0o600)
    def call(self,status=200,data=None,headers=None,**changes):
        reply=Mock(); reply.status=status
        raw=json.dumps(data if data is not None else {'text':'hello'}).encode()
        h={'Content-Length':str(len(raw)),**(headers or {})}
        reply.getheader.side_effect=lambda k,default=None:h.get(k,default)
        reply.read1.side_effect=[raw,b'']
        connection=Mock(); connection.getresponse.return_value=reply
        if changes.pop('connection_close', False):
            def close_socket():
                connection.sock=None
                return reply
            connection.getresponse.side_effect=close_socket
        with patch('mcp_groq.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch('mcp_groq.http.client.HTTPConnection',return_value=connection):
            kwargs=dict(size=5,extension='ogg',mime_type='audio/ogg',language='auto',deadline=time.monotonic()+60)
            kwargs.update(changes)
            output=GroqClient(self.key,'x'*48).transcribe(NoBulkRead(b'audio'),**kwargs)
        return output,connection
    def test_connection_close_can_read_successful_response(self):
        output,_=self.call(connection_close=True)
        self.assertEqual(output['text'],'hello')
    def test_multipart_streams_fixed_model(self):
        result,connection=self.call()
        self.assertEqual(result['text'],'hello')
        body=b''.join(c.args[0] for c in connection.send.call_args_list)
        self.assertIn(b'whisper-large-v3',body); self.assertIn(b'filename="audio.ogg"',body)
        self.assertIn(b'audio',body)
        lengths=[c.args[1] for c in connection.putheader.call_args_list if c.args[0]=='Content-Length']
        self.assertEqual(int(lengths[0]),len(body))
        self.assertLess(len(body)-5,16384)
    def test_rate_limit_and_secret_not_in_error(self):
        with self.assertRaises(TranscriptionError) as error: self.call(429,{'message':'sensitive'}, {'Retry-After':'12'})
        self.assertEqual(error.exception.code,'provider_rate_limited')
        self.assertEqual(error.exception.retry_after,12)
        self.assertNotIn('sensitive',str(error.exception))
        self.assertNotIn('gsk_',str(error.exception))
    def test_redirect_response_limit_and_input_limits(self):
        for status,data in [(302,{}),(200,{'text':'x'*262145})]:
            with self.assertRaises(TranscriptionError): self.call(status,data)
        for changes in ({'size':10485761},{'extension':'../unsafe'},{'language':'not-a-language'}):
            with self.assertRaises(TranscriptionError): self.call(**changes)
    def test_private_dns_and_timeout_never_retry(self):
        client=GroqClient(self.key,'x'*48)
        with patch('mcp_groq.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch('mcp_groq.http.client.HTTPConnection') as connection:
            with self.assertRaises(TranscriptionError): client.transcribe(io.BytesIO(b'a'),size=1,extension='ogg',mime_type='audio/ogg',language='auto',deadline=time.monotonic()+60)
            connection.assert_not_called()
        connection=Mock(); connection.getresponse.side_effect=TimeoutError()
        with patch('mcp_groq.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch('mcp_groq.http.client.HTTPConnection',return_value=connection) as factory:
            with self.assertRaisesRegex(TranscriptionError,'transcription_uncertain'):
                client.transcribe(io.BytesIO(b'a'),size=1,extension='ogg',mime_type='audio/ogg',language='auto',deadline=time.monotonic()+60)
            self.assertEqual(factory.call_count,1)
    def test_proxy_fixed_route(self):
        text=groq_proxy_config()
        self.assertIn('listen 127.0.0.1:8095;',text)
        self.assertIn('proxy_ssl_name api.groq.com;',text)
        self.assertIn('location = /openai/v1/audio/transcriptions',text)
        self.assertIn('limit_except POST',text)
        self.assertIn('proxy_request_buffering off;',text)
        self.assertNotIn('listen 0.0.0.0',text)


if __name__=='__main__': unittest.main()

"""One fixed Groq endpoint, streaming multipart, no redirects or retries."""
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path
from mcp_transcription_store import TranscriptionError

MODEL = 'whisper-large-v3'
ENDPOINT = '/openai/v1/audio/transcriptions'


class GroqRejected(TranscriptionError):
    """An explicit 4xx rejected this attempt; its reservation is still retained."""


class GroqClient:
    def __init__(self, key_path, proxy_token, *, clock=time.monotonic):
        path = Path(key_path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024:
            raise TranscriptionError('transcription_not_configured')
        if os.name != 'nt' and path.stat().st_mode & 0o077:
            raise TranscriptionError('transcription_not_configured')
        value = path.read_text().strip()
        if not re.fullmatch(r'gsk_[A-Za-z0-9]{20,200}', value):
            raise TranscriptionError('transcription_not_configured')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', proxy_token) or value == proxy_token:
            raise TranscriptionError('transcription_not_configured')
        self.key, self.proxy_token, self.clock = value, proxy_token, clock

    def transcribe(self, source, *, size, extension, mime_type, language, deadline):
        if (type(size) is not int or not 0 < size <= 10485760
                or extension not in ('ogg','mp3','wav','flac','m4a','webm','mpeg')
                or not re.fullmatch(r'audio/[A-Za-z0-9.+-]{1,40}', mime_type)
                or language != 'auto' and not re.fullmatch(r'[a-z]{2}', language)):
            raise TranscriptionError('unsupported_audio')
        def timeout(maximum):
            remaining = deadline - self.clock()
            if remaining <= 0: raise TranscriptionError('transcription_uncertain')
            return min(maximum, remaining)
        boundary = 'lylahub-' + secrets.token_hex(16)
        fields = [('model',MODEL),('response_format','json'),('temperature','0')]
        if language != 'auto': fields.append(('language',language))
        head = ''.join('--'+boundary+'\r\nContent-Disposition: form-data; name="'+k+'"\r\n\r\n'+v+'\r\n' for k,v in fields)
        head += '--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="audio.'+extension+'"\r\nContent-Type: '+mime_type+'\r\n\r\n'
        head = head.encode(); tail = ('\r\n--'+boundary+'--\r\n').encode()
        connection, reply = None, None
        try:
            timeout(3)
            addresses = {r[4][0] for r in socket.getaddrinfo('api.groq.com',443,socket.AF_INET,socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise TranscriptionError('transcription_unavailable')
            connection = http.client.HTTPConnection('127.0.0.1',8095,timeout=timeout(3))
            headers = {'Authorization':'Bearer '+self.key, 'Content-Type':'multipart/form-data; boundary='+boundary,
                'Content-Length':str(size+len(head)+len(tail)), 'Accept-Encoding':'identity',
                'X-LylaHub-Groq-IP':sorted(addresses)[0], 'X-LylaHub-Groq-Token':self.proxy_token}
            connection.putrequest('POST',ENDPOINT)
            for key,value in headers.items(): connection.putheader(key,value)
            connection.endheaders(); connection.sock.settimeout(timeout(5)); connection.send(head)
            source.seek(0); remaining = size
            while remaining:
                connection.sock.settimeout(timeout(5))
                chunk = source.read(min(65536,remaining))
                if not chunk: raise TranscriptionError('transcription_uncertain')
                connection.send(chunk); remaining -= len(chunk)
            connection.send(tail)
            response_socket = connection.sock
            response_socket.settimeout(timeout(45)); reply = connection.getresponse()
            if reply.status == 429:
                retry = reply.getheader('Retry-After','60')
                retry = min(86400,max(1,int(retry))) if retry.isdigit() else 60
                raise GroqRejected('provider_rate_limited',retry)
            if 400 <= reply.status < 500: raise GroqRejected('transcription_unavailable')
            if reply.status != 200 or reply.getheader('Content-Encoding','identity') != 'identity':
                raise TranscriptionError('transcription_uncertain')
            declared = reply.getheader('Content-Length')
            if declared is not None and (not declared.isdigit() or int(declared) > 262144):
                raise TranscriptionError('transcription_uncertain')
            output = bytearray()
            while True:
                if reply.isclosed() is True: break
                response_socket.settimeout(timeout(5))
                chunk = reply.read1(min(65536,262145-len(output)))
                if not chunk: break
                output.extend(chunk)
                if len(output)>262144: raise TranscriptionError('transcription_uncertain')
            if declared is not None and len(output)!=int(declared): raise TranscriptionError('transcription_uncertain')
            data = json.loads(output)
            if not isinstance(data,dict) or not isinstance(data.get('text'),str):
                raise TranscriptionError('transcription_uncertain')
            result = {'text':data['text'],'model':MODEL}
            if len(json.dumps(result,ensure_ascii=True).encode()) > 260000:
                raise TranscriptionError('transcription_uncertain')
            return result
        except (OSError,http.client.HTTPException,ValueError,UnicodeError) as error:
            if isinstance(error,TranscriptionError): raise
            raise TranscriptionError('transcription_uncertain') from None
        finally:
            if reply is not None: reply.close()
            if connection: connection.close()

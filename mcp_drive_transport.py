"""Bounded Drive HTTPS via a token-protected loopback fixed-host Nginx proxy."""
import http.client
import ipaddress
import json
import re
import socket
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit


class DriveError(Exception):
    def __init__(self, code='drive_unavailable', status=None):
        super().__init__(code)
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict
    data: bytes

    def json(self):
        try:
            value = json.loads(self.data)
            if not isinstance(value, dict): raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise DriveError('drive_invalid_response') from None


class DriveTransport:
    def __init__(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
            raise ValueError('drive_proxy_credential_required')
        self.token = token
        self.local = threading.local()

    @contextmanager
    def scope(self, seconds):
        previous = getattr(self.local, 'deadline', float('inf'))
        self.local.deadline = min(previous, time.monotonic() + seconds)
        try: yield
        finally: self.local.deadline = previous

    def request(self, method, path, *, headers=None, body=None, size=None, maximum=1048576, sink=None):
        deadline = min(time.monotonic() + 60, getattr(self.local, 'deadline', float('inf')))
        if time.monotonic() >= deadline: raise DriveError('drive_timeout')
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not isinstance(maximum, int)
                or not 1 <= maximum <= 10485760 or re.search(r'[\s\x00-\x1f\x7f\\]', path)):
            raise DriveError('drive_invalid_request')
        if path == '/token' and method == 'POST':
            host = 'oauth2.googleapis.com'
        elif (method in ('GET', 'POST', 'PATCH') and re.fullmatch(
                r'/api/drive/v3/(about|files|files/generateIds|files/[A-Za-z0-9_-]{1,200})', parsed.path)):
            host = 'www.googleapis.com'
        elif (method in ('POST', 'PATCH', 'PUT') and re.fullmatch(
                r'/upload/drive/v3/files(?:/[A-Za-z0-9_-]{1,200})?', parsed.path)):
            host = 'www.googleapis.com'
        else:
            raise DriveError('drive_invalid_request')
        if body is None: body = b''
        if isinstance(body, bytes): size = len(body)
        if type(size) is not int or not 0 <= size <= 10485760:
            raise DriveError('drive_request_limit')
        connection = None
        try:
            addresses = {record[4][0] for record in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise DriveError('drive_private_destination')
            outgoing = dict(headers or {})
            outgoing.update({'Content-Length': str(size), 'Accept-Encoding': 'identity',
                             'X-LylaHub-Drive-IP': sorted(addresses)[0], 'X-LylaHub-Drive-Token': self.token})
            if any(not isinstance(v, str) or re.search(r'[\r\n]', k + v) for k, v in outgoing.items()):
                raise DriveError('drive_invalid_request')
            connection = http.client.HTTPConnection('127.0.0.1', 8094, timeout=max(.01, min(5, deadline - time.monotonic())))
            connection.putrequest(method, path)
            for key, value in outgoing.items(): connection.putheader(key, value)
            connection.endheaders()
            if isinstance(body, bytes): connection.send(body)
            else:
                remaining = size
                while remaining:
                    if time.monotonic() >= deadline: raise DriveError('drive_timeout')
                    connection.sock.settimeout(max(.01, min(5, deadline - time.monotonic())))
                    chunk = body.read(min(65536, remaining))
                    if not chunk: raise DriveError('drive_truncated_upload')
                    connection.send(chunk); remaining -= len(chunk)
            reply = connection.getresponse()
            if reply.getheader('Content-Encoding', 'identity') != 'identity': raise DriveError('drive_invalid_response')
            ceiling = maximum if 200 <= reply.status < 300 else min(maximum, 4096)
            declared = reply.getheader('Content-Length')
            if declared and (not declared.isdigit() or int(declared) > ceiling): raise DriveError('drive_response_limit')
            output = bytearray()
            received = 0
            while True:
                if time.monotonic() >= deadline: raise DriveError('drive_timeout')
                if connection.sock: connection.sock.settimeout(max(.01, min(5, deadline - time.monotonic())))
                chunk = reply.read1(min(65536, ceiling + 1 - received))
                if not chunk: break
                received += len(chunk)
                if received > ceiling: raise DriveError('drive_response_limit')
                if sink is not None and 200 <= reply.status < 300: sink.write(chunk)
                else: output.extend(chunk)
            if declared is not None and received != int(declared): raise DriveError('drive_invalid_response')
            # Never follow Location automatically, including redirects carrying credentials.
            return Response(reply.status, {k.lower(): v for k, v in reply.getheaders()}, bytes(output))
        except (OSError, http.client.HTTPException):
            raise DriveError() from None
        finally:
            if connection: connection.close()

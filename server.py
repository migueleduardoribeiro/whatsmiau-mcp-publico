"""Small loopback-only bridge. Nginx owns authentication and static assets."""
import json
import base64
import http.client
import time
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import threading
import urllib.error
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EVENTS = ('MESSAGES_UPSERT', 'MESSAGES_UPDATE', 'MESSAGES_DELETE', 'MESSAGES_SET',
          'CONTACTS_UPSERT', 'GROUP_PARTICIPANTS_UPDATE', 'CONNECTION_UPDATE', 'CALL')
MCP_PATHS = frozenset(('/mcp', '/oauth/authorize', '/oauth/token', '/oauth/revoke',
                       '/.well-known/oauth-authorization-server', '/.well-known/oauth-protected-resource',
                       '/.well-known/oauth-protected-resource/mcp'))


def gateway_url(identifier):
    return 'http://192.0.2.10:8081/webhooks/' + identifier


def validate_url(value, identifier):
    if not isinstance(value, str) or len(value) > 1800 or re.search(r'[\s\x00-\x1f\x7f\\]', value):
        raise APIError(400, 'URL inválida.')
    if value == gateway_url(identifier):
        return
    try:
        url = urllib.parse.urlsplit(value)
        host = url.hostname or ''
        if url.scheme != 'https' or url.username is not None or url.password is not None or url.query or url.fragment or url.port not in (None, 443):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or '.' not in host or host.endswith(('.local', '.internal', '.localhost')) or re.fullmatch(r'[0-9.]+', host):
                raise ValueError() from None
    except ValueError:
        raise APIError(400, 'Use HTTPS público sem credenciais, parâmetros ou fragmentos, ou o gateway LylaHub desta instância.') from None


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Bridge:
    def __init__(self, api_key, upstream, timeout):
        self.key, self.upstream, self.timeout = api_key, upstream.rstrip('/'), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, method='GET', body=None, timeout=None):
        request = urllib.request.Request(
            self.upstream + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'apikey': self.key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise APIError(502, 'A resposta do serviço excedeu o limite do painel.')
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 504:
                raise APIError(504, 'O servico de origem nao respondeu dentro do prazo.') from None
            if error.code == 404:
                raise APIError(404, 'Instância não encontrada.') from None
            if error.code in (400, 409, 422):
                raise APIError(error.code, 'Não foi possível concluir. Confira o nome e se a instância já existe.') from None
            raise APIError(502, 'O WhatsMiau não conseguiu atender. Tente novamente.') from None
        except (TimeoutError, socket.timeout):
            raise APIError(504, 'O WhatsMiau demorou a responder. Tente novamente.') from None
        except (urllib.error.URLError, ValueError):
            raise APIError(502, 'O WhatsMiau está indisponível no momento.') from None

    def call_media(self, path, metadata, data, mime_type):
        """Stream a data URI to the existing loopback API without full base64 copies."""
        target = urllib.parse.urlsplit(self.upstream)
        if (target.scheme != 'http' or target.hostname != '127.0.0.1' or target.username
                or target.password or target.path or target.query or target.fragment):
            raise APIError(502, 'Invalid media upstream.')
        prefix = json.dumps(metadata, ensure_ascii=True).encode()[:-1] + b',"media":"data:' + mime_type.encode('ascii') + b';base64,'
        suffix = b'"}'
        length = len(prefix) + 4 * ((len(data) + 2) // 3) + len(suffix)
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=5)
        deadline = time.monotonic() + 35
        try:
            connection.putrequest('POST', path)
            for name, value in {'apikey': self.key, 'Content-Type': 'application/json',
                                'Accept': 'application/json', 'Content-Length': str(length)}.items():
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(prefix)
            for offset in range(0, len(data), 49152):
                if time.monotonic() > deadline:
                    raise TimeoutError()
                connection.send(base64.b64encode(memoryview(data)[offset:offset + 49152]))
            connection.send(suffix)
            response = connection.getresponse()
            if response.status != 200:
                raise APIError(502, 'Media send failed.')
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise APIError(502, 'Media response too large.')
            return json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException):
            raise APIError(502, 'Media send unavailable.') from None
        finally:
            connection.close()

    def state(self, identifier):
        try:
            response = self.call('/v1/instance/connectionState/' + identifier, timeout=min(self.timeout, 1.5))
            state = response.get('state') or response.get('instance', {}).get('state') or 'unknown'
            if state == 'closed':
                state = 'close'
            return state if state in ('open', 'close', 'connecting', 'connected', 'disconnected') else 'unknown'
        except (APIError, AttributeError):
            return 'unknown'

    def instances(self):
        response = self.call('/v1/instance/fetchInstances')
        if not isinstance(response, list):
            raise APIError(502, 'O WhatsMiau retornou uma lista inválida.')
        instances = []
        for item in response:
            if not isinstance(item, dict) or not valid_id(item.get('id')):
                continue
            instances.append({'id': item['id'], 'name': str(item.get('instanceName') or item['id']),
                              'number': str(item.get('ownerJid') or item.get('remoteJID') or '').split('@')[0],
                              'state': 'unknown'})
        # Avoid hundreds of state requests if this small VM grows beyond its intended size.
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(self.state, [i['id'] for i in instances[:20]]))
        for instance, state in zip(instances, states):
            instance['state'] = state
        return {'instances': instances}

    def create(self, body):
        name = body.get('name')
        if not isinstance(name, str):
            raise APIError(400, 'Informe um nome para a instância.')
        name = name.strip()
        if not re.fullmatch(r'[\w .-]{1,80}', name, re.UNICODE):
            raise APIError(400, 'Use até 80 caracteres: letras, números, espaços, ponto, hífen ou sublinhado.')
        response = self.call('/v1/instance/create', 'POST',
                             {'instanceName': name, 'syncFullHistory': False, 'readMessages': False})
        if not isinstance(response, dict) or not valid_id(response.get('id')):
            raise APIError(502, 'A criação retornou um resultado inválido. Atualize a lista antes de tentar novamente.')
        return {'id': response['id'], 'name': name}

    def connect(self, identifier):
        response = self.call('/v1/instance/connect/' + identifier)
        if not isinstance(response, dict):
            raise APIError(502, 'Não foi possível obter o QR code.')
        result = {'connected': response.get('connected') is True}
        qr = response.get('base64')
        if isinstance(qr, str) and qr:
            qr = qr.removeprefix('data:image/png;base64,')
            if len(qr) < 1024 * 1024 and re.fullmatch(r'[A-Za-z0-9+/=\r\n]+', qr):
                result['base64'] = qr
        return result

    def instance(self, identifier):
        items = self.call('/v1/instance/fetchInstances')
        if not isinstance(items, list):
            raise APIError(502, 'Lista de instâncias inválida.')
        for item in items:
            if isinstance(item, dict) and item.get('id') == identifier:
                return item
        raise APIError(404, 'Instância não encontrada.')

    def details(self, identifier, origin):
        item = self.instance(identifier)
        return {'id': identifier, 'name': str(item.get('instanceName') or identifier),
                'api_url': origin + '/v1', 'credential_scope': 'global', 'credential_header': 'apikey'}

    def webhook(self, identifier):
        response = self.call('/v1/webhook/find/' + identifier)
        if not isinstance(response, dict) or not isinstance(response.get('webhook'), dict):
            raise APIError(502, 'Configuração de webhook inválida.')
        raw = response['webhook']
        url, events = raw.get('url', ''), raw.get('events') or []
        if not isinstance(url, str) or not isinstance(events, list) or any(not isinstance(e, str) for e in events):
            raise APIError(502, 'Configuração de webhook inválida.')
        normalized = [e.strip().upper().replace('.', '_').replace('-', '_') for e in events]
        try:
            parsed = urllib.parse.urlsplit(url)
            sensitive_url = bool(parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or (self.key and self.key in url))
        except ValueError:
            sensitive_url = True
        # nil Enabled emits in v1.5.0; nil Base64 is false. Do not expose header values.
        config = {'url': '' if sensitive_url else url, 'enabled': raw.get('enabled') is not False,
                  'base64': raw.get('base64') is True, 'events': normalized}
        revision = hmac.new(self.key.encode(), json.dumps(raw, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return {'webhook': config, 'revision': revision, 'supported_events': list(EVENTS),
                'unsupported_events': [e for e in normalized if e not in EVENTS],
                'managed': url == gateway_url(identifier), 'sensitive_url': sensitive_url,
                'capabilities': {'scope': 'instance', 'base64': True, 'headers': False}}

    def set_webhook(self, identifier, body):
        if set(body) != {'url', 'enabled', 'base64', 'events', 'revision'}:
            raise APIError(400, 'Campos de webhook inválidos. Headers não têm efeito nesta versão.')
        if type(body['enabled']) is not bool or type(body['base64']) is not bool:
            raise APIError(400, 'Ativação e base64 devem ser booleanos.')
        events = body['events']
        if not isinstance(events, list) or any(not isinstance(e, str) or e not in EVENTS for e in events) or len(events) != len(set(events)):
            raise APIError(400, 'Selecione somente eventos suportados, sem repetições.')
        if body['enabled'] and not events:
            raise APIError(400, 'Selecione pelo menos um evento para ativar o webhook.')
        validate_url(body['url'], identifier)
        if self.key and self.key in body['url']:
            raise APIError(400, 'Não inclua a API key na URL.')
        current = self.webhook(identifier)
        if body['revision'] != current['revision']:
            raise APIError(409, 'O webhook mudou desde a consulta. Feche e abra o formulário para recarregar.')
        if current['managed'] or body['url'] == gateway_url(identifier):
            if body['url'] != gateway_url(identifier) or not body['enabled'] or body['base64'] or 'MESSAGES_UPSERT' not in events:
                raise APIError(400, 'Preserve o gateway LylaHub ativo com MESSAGES_UPSERT e sem base64.')
        config = {k: body[k] for k in ('url', 'enabled', 'base64', 'events')}
        self.call('/v1/webhook/set/' + identifier, 'POST', {'webhook': config})
        saved = self.webhook(identifier)
        if saved['webhook'] != config:
            raise APIError(502, 'O serviço não confirmou a configuração salva. Reabra o formulário antes de tentar novamente.')
        return saved

    def remove(self, identifier, action, body):
        if action not in ('logout', 'delete') or body.get('confirm') is not True:
            raise APIError(400, 'Confirme explicitamente a operação.')
        item = self.instance(identifier)
        if body.get('name') != str(item.get('instanceName') or identifier):
            raise APIError(400, 'A confirmação deve corresponder ao nome atual da instância.')
        self.call('/v1/instance/' + action + '/' + identifier, 'DELETE')
        return {'ok': True}


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) is not None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log credentials, QR codes, or user supplied request paths.

    def reply(self, status, body):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def dispatch(self):
        archive_path = getattr(self, 'path', '').partition('?')[0]
        if archive_path in ('/drive/oauth/start', '/drive/oauth/callback', '/drive/status') or archive_path.startswith('/archive/events/'):
            return self.dispatch_archive()
        if getattr(self, 'path', '').partition('?')[0] in MCP_PATHS:
            return self.dispatch_mcp()
        consumed = False
        try:
            if self.headers.get('X-LylaHub-Panel') != '1' or not self.headers.get('X-LylaHub-User'):
                raise APIError(403, 'Requisição não autorizada pelo painel.')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise APIError(403, 'Origem não autorizada.')
            if self.command != 'GET' and self.headers.get('Origin') != self.server.origin:
                raise APIError(403, 'Origem não autorizada.')
            if self.command == 'GET' and self.path == '/instances':
                return self.reply(200, self.server.bridge.instances())
            match = re.fullmatch(r'/instances/([A-Za-z0-9_-]{1,100})/(connect|webhook|details|logout|delete)', self.path)
            if self.command == 'GET' and match:
                identifier, action = match.groups()
                if action == 'webhook':
                    return self.reply(200, self.server.bridge.webhook(identifier))
                if action == 'details':
                    return self.reply(200, self.server.bridge.details(identifier, self.server.origin))
            if self.command != 'POST' or (self.path not in ('/instances', '/credentials/reveal') and (not match or match[2] == 'details')):
                raise APIError(404, 'Operação não disponível no painel.')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise APIError(400, 'Envie dados no formato JSON.')
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise APIError(400, 'Dados inválidos.') from None
            if length > 4096:
                raise APIError(413, 'Dados enviados excedem o limite.')
            if length <= 0 or self.headers.get('Transfer-Encoding'):
                raise APIError(400, 'Dados inválidos.')
            try:
                raw = self.rfile.read(length)
                consumed = True
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise APIError(400, 'JSON inválido.') from None
            if not isinstance(body, dict):
                raise APIError(400, 'Dados inválidos.')
            with self.server.mutations:
                if self.path == '/credentials/reveal':
                    if body != {'confirm': True}:
                        raise APIError(400, 'Confirme a revelação da chave global.')
                    return self.reply(200, {'api_key': self.server.bridge.key, 'scope': 'global'})
                if match:
                    identifier, action = match.groups()
                    if action == 'connect':
                        return self.reply(200, self.server.bridge.connect(identifier))
                    if action == 'webhook':
                        return self.reply(200, self.server.bridge.set_webhook(identifier, body))
                    return self.reply(200, self.server.bridge.remove(identifier, action, body))
                return self.reply(201, self.server.bridge.create(body))
        except APIError as error:
            # Closing a TCP connection with unread data can reset it before the
            # client receives the 4xx. Drain only bounded, non-chunked bodies.
            if not consumed and not self.headers.get('Transfer-Encoding'):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if 0 < length <= 65536:
                        if getattr(self, 'connection', None) is not None:
                            self.connection.settimeout(2)
                        self.rfile.read(length)
                except (ValueError, OSError):
                    pass
            self.reply(error.status, {'error': error.message})
        except Exception:
            self.reply(502, {'error': 'Não foi possível concluir. Tente atualizar a lista.'})

    def dispatch_mcp(self):
        self.close_connection = True
        try:
            self.connection.settimeout(5)
            if self.server.mcp is None:
                return self.reply(404, {'error': 'not_found'})
            lengths = self.headers.get_all('Content-Length', [])
            if (len(lengths) > 1 or self.headers.get('Transfer-Encoding')
                    or len(self.headers.get_all('Host', [])) != 1
                    or len(self.headers.get_all('Authorization', [])) > 1):
                return self.reply(400, {'error': 'invalid_request'})
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                return self.reply(400, {'error': 'invalid_request'})
            if length > 32768:
                # Drain only small, explicitly framed rejected bodies so closing
                # the socket does not reset the connection before the 413 arrives.
                if length <= 65536:
                    self.rfile.read(length)
                return self.reply(413, {'error': 'request_too_large'})
            if self.command == 'GET' and length:
                return self.reply(400, {'error': 'invalid_request'})
            body = self.rfile.read(length) if length else b''
            if len(body) != length:
                return self.reply(400, {'error': 'invalid_request'})
            status, headers, data = self.server.mcp.handle(self.command, self.path, dict(self.headers), body)
            encoded = data if isinstance(data, bytes) else b'' if data is None else json.dumps(data, ensure_ascii=False).encode()
            content = data.get('result', {}).get('content', []) if isinstance(data, dict) else []
            has_image = isinstance(content, list) and any(isinstance(item, dict) and item.get('type') == 'image' for item in content)
            if len(encoded) > (8 if has_image else 4) * 1024 * 1024:
                return self.reply(502, {'error': 'response_too_large'})
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(encoded)
        except (ValueError, TimeoutError):
            self.reply(400, {'error': 'invalid_request'})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def dispatch_archive(self):
        handler = getattr(self.server, 'archive_http', None)
        if handler is None: return self.reply(404, {'error': 'archive_unavailable'})
        try:
            expected_host = urllib.parse.urlsplit(self.server.mcp.oauth.issuer).netloc
            lengths = self.headers.get_all('Content-Length', [])
            if (len(self.path) > 8192 or self.headers.get_all('Host', []) != [expected_host]
                    or self.headers.get('Transfer-Encoding') or len(lengths) > 1
                    or any(len(self.headers.get_all(name, [])) > 1 for name in (
                        'X-LylaHub-Archive-Hook', 'X-LylaHub-Archive-Token', 'X-LylaHub-Archive-Login', 'X-LylaHub-User'))):
                return self.reply(400, {'error': 'invalid_request'})
            length = int(lengths[0]) if lengths else 0
            maximum = 262144 if self.path.startswith('/archive/events/') else 16384
            if length < 0 or self.command == 'GET' and length: return self.reply(400, {'error': 'invalid_request'})
            if length > maximum:
                if length <= maximum + 65536: self.rfile.read(length)
                return self.reply(413, {'error': 'request_too_large'})
            body = self.rfile.read(length) if length else b''
            if len(body) != length: return self.reply(400, {'error': 'invalid_request'})
            status, headers, data = handler.handle(self.command, self.path, dict(self.headers), body)
            encoded = data if isinstance(data, bytes) else b'' if data is None else json.dumps(data, ensure_ascii=False).encode()
            if len(encoded) > 65536: return self.reply(502, {'error': 'response_too_large'})
            self.send_response(status)
            for key, value in headers.items(): self.send_header(key, value)
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Connection', 'close')
            self.end_headers(); self.wfile.write(encoded)
        except (ValueError, TimeoutError): self.reply(400, {'error': 'invalid_request'})
        except (BrokenPipeError, ConnectionResetError): pass

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch
    do_PUT = dispatch
    do_OPTIONS = dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(20)
        return request, address

    def server_close(self):
        if getattr(self, 'archive_runtime', None): self.archive_runtime.stop()
        if getattr(self, 'conversation_store', None):
            self.conversation_store.close(); self.conversation_store=None
        super().server_close()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def configure_mcp(config, bridge):
    from mcp_oauth import OAuth
    from mcp_protocol import MCPApplication
    from mcp_store import Store
    from mcp_tools import Tools
    required = ('issuer', 'instance_id', 'allowed_user', 'clients', 'state_path')
    if (not isinstance(config, dict) or any(not config.get(key) for key in required if key != 'clients')
            or any(not isinstance(config[key], str) for key in required if key != 'clients')
            or not isinstance(config.get('clients'), dict)):
        raise ValueError('Invalid MCP configuration')
    if type(config.get('enable_media', False)) is not bool:
        raise ValueError('Invalid media setting')
    conversation_settings=config.get('conversations',{})
    if (not isinstance(conversation_settings,dict) or set(conversation_settings)-{'enabled','unread_mode','read_state_source','mark_presented_read'}
            or type(conversation_settings.get('enabled',False)) is not bool
            or type(conversation_settings.get('mark_presented_read',False)) is not bool
            or conversation_settings.get('unread_mode','unknown') not in ('unknown','available_with_warning')
            or conversation_settings.get('read_state_source','unknown') not in ('unknown','phone_on_demand')):
        raise ValueError('Invalid conversations setting')
    if conversation_settings.get('enabled') and config.get('archive') is None:
        raise ValueError('Conversations require existing archive')
    if config.get('enable_media') and not config.get('media_fetch_token'):
        raise ValueError('Media proxy credential required')
    url = urllib.parse.urlsplit(config['issuer'])
    if (url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment
            or url.username or url.password or url.port not in (None, 443)):
        raise ValueError('Invalid MCP issuer')
    for client_id, client in config['clients'].items():
        if (not isinstance(client_id, str) or not isinstance(client, dict)
                or not isinstance(client.get('redirect_uris'), list) or not client['redirect_uris']
                or not all(isinstance(uri, str) and uri.startswith('https://') and not urllib.parse.urlsplit(uri).fragment
                           for uri in client['redirect_uris'])
                or not isinstance(client.get('secret', ''), str)):
            raise ValueError('Invalid MCP client configuration')
    store = Store(config['state_path'])
    try:
        oauth = OAuth(store, config['issuer'], config['issuer'] + '/mcp', config['clients'],
                      config['instance_id'], config['allowed_user'])
        files = None
        if config.get('enable_media'):
            import cryptography  # Fail closed before advertising unsupported media tools.
            from mcp_files import FileGateway
            from mcp_media import MediaProvider
            files = FileGateway(config, store, MediaProvider(bridge, config))
        archive = None
        if config.get('archive') is not None:
            if not isinstance(config['archive'], dict) or files is None:
                raise ValueError('Archive requires configured media transport')
            from mcp_archive_runtime import ArchiveRuntime
            archive = ArchiveRuntime(config, bridge, store, files.provider)
            files.archive = archive.query
            files.send_observer = archive.record_accepted_send
        vision = config.get('vision', {})
        if not isinstance(vision, dict) or type(vision.get('enabled', False)) is not bool:
            raise ValueError('Invalid vision setting')
        if vision.get('enabled'):
            settings = config.get('transcription') or {}
            if (files is None or not settings.get('enabled') or not vision.get('authorized_at')
                    or not vision.get('free_plan_verified_at')):
                raise ValueError('Vision requires authorization and verified free plan')
            from mcp_vision import GroqVisionClient, ImageAnalyzer
            files.image_analyzer = ImageAnalyzer(GroqVisionClient(settings['key_path'], settings['proxy_token']), store)
        conversations=None
        if conversation_settings.get('enabled'):
            from mcp_conversation_store import ConversationStore
            from mcp_conversation_source import ConversationSource
            from mcp_conversation_catalog import ConversationCatalog
            from mcp_conversation_query import ConversationQuery
            from mcp_read_coordinator import ReadCoordinator
            conversation_store=ConversationStore(Path(config['state_path']).with_name('conversations.db'))
            source=ConversationSource(bridge,store.profile(config['allowed_user']),config['instance_id'],
                phone_state=conversation_settings.get('read_state_source')=='phone_on_demand',
                receipts_enabled=conversation_settings.get('mark_presented_read',False))
            catalog=ConversationCatalog(archive.query,source)
            fallback=conversation_settings.get('unread_mode','unknown')
            conversations=dict(catalog=catalog,query=ConversationQuery(archive.query,catalog,
                unread_fallback=fallback=='available_with_warning'),
                reader=ReadCoordinator(archive.query,source,conversation_store))
        app = MCPApplication(oauth, Tools(bridge, store, config, file_gateway=files,
            archive=archive.query if archive else None, transfer_slot=archive.transfer_slot if archive else None,
            send_observer=archive.record_accepted_send if archive else None,
            transcriber=archive.transcriber if archive else None,conversations=conversations))
        app.conversation_store=conversation_store if conversations else None
        app.archive_runtime = archive
        return app
    except Exception:
        store.close()
        raise


def make_server(address, api_key, upstream='http://127.0.0.1:8080',
                origin='https://whatsmiau.example.org', timeout=10, mcp_config=None):
    bridge = Bridge(api_key, upstream, timeout)
    mcp = configure_mcp(mcp_config, bridge) if mcp_config is not None else None
    try:
        server = Server(address, Handler)
    except Exception:
        if mcp:
            mcp.oauth.store.close()
        raise
    server.origin = origin
    server.bridge = bridge
    server.mcp = mcp
    server.archive_runtime = getattr(mcp, 'archive_runtime', None)
    server.conversation_store = getattr(mcp, 'conversation_store', None)
    server.archive_http = None
    if server.archive_runtime:
        from mcp_archive_http import ArchiveHTTP
        server.archive_http = ArchiveHTTP(server.archive_runtime)
        server.archive_runtime.start()
    server.mutations = threading.Lock()
    return server


if __name__ == '__main__':
    credential = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'whatsmiau.env'
    key = next(line.split('=', 1)[1].strip() for line in credential.read_text().splitlines()
               if line.startswith('API_KEY='))
    if not key:
        raise SystemExit('API credential missing')
    config_path = os.environ.get('LYLA_MCP_CONFIG')
    mcp_config = json.loads(Path(config_path).read_text()) if config_path else None
    make_server(('127.0.0.1', 8090), key, mcp_config=mcp_config).serve_forever()

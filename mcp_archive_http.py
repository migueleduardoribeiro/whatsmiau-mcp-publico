"""Small isolated HTTP boundary for Google consent and private event ingress."""
import hmac
import html
import re
from urllib.parse import urlsplit, parse_qs
from mcp_drive_transport import DriveError
from mcp_archive_store import ArchiveFull
from mcp_store import StateError


class ArchiveHTTP:
    def __init__(self, runtime): self.runtime = runtime

    def handle(self, method, path, headers, body):
        headers = {key.lower(): value for key, value in headers.items()}
        common = {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
                  'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'}
        try:
            parsed = urlsplit(path)
            route = parsed.path
            hook = re.fullmatch(r'/archive/events/([A-Za-z0-9_-]{1,100})', route)
            if hook:
                if (method != 'POST' or parsed.query or headers.get('x-lylahub-archive-hook') != '1'
                        or not hmac.compare_digest(headers.get('x-lylahub-archive-token', ''), self.runtime.settings['hook_token'])):
                    return 403, common, {'error': 'access_denied'}
                if headers.get('content-type', '').split(';')[0] != 'application/json' or len(body) > 262144:
                    return 400, common, {'error': 'invalid_request'}
                return 202, common, self.runtime.accept_webhook(hook[1], body)
            if route == '/drive/oauth/callback':
                if method != 'GET' or body: return 400, common, {'error': 'invalid_request'}
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (any(len(v) != 1 for v in query.values()) or set(query) - {
                        'state', 'code', 'scope', 'authuser', 'prompt', 'error', 'error_description', 'iss'}
                        or 'iss' in query and query['iss'] != ['https://accounts.google.com']):
                    return 400, common, {'error': 'invalid_request'}
                self.runtime.oauth.complete({k: v[0] for k, v in query.items()})
                return 303, dict(common, Location='/drive/status'), None
            if (headers.get('x-lylahub-archive-login') != '1'
                    or headers.get('x-lylahub-user') != self.runtime.config['allowed_user']
                    or method != 'GET' and headers.get('sec-fetch-site') == 'cross-site'):
                return 403, common, {'error': 'access_denied'}
            if route == '/drive/status' and method == 'GET' and not parsed.query:
                return 200, common, self.runtime.health()
            if route != '/drive/oauth/start' or parsed.query: return 404, common, {'error': 'not_found'}
            owner = self.runtime.config['allowed_user']
            if method == 'GET' and not body:
                csrf = self.runtime.mcp_store.ticket('drive_start_csrf', {'owner': owner}, 600)
                content = ('<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Google Drive</title>'
                    '<h1>Conectar arquivo WhatsMiau ao Google Drive</h1><p>Autorize os arquivos criados por este aplicativo.</p>'
                    '<form method="post" action="/drive/oauth/start"><input type="hidden" name="csrf" value="'
                    + html.escape(csrf, quote=True) + '"><button>Conectar Google Drive</button></form></html>').encode()
                common.update({'Content-Type': 'text/html; charset=utf-8', 'Referrer-Policy': 'same-origin',
                    'Content-Security-Policy': "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com/o/oauth2/v2/auth"})
                return 200, common, content
            if method == 'POST':
                if headers.get('origin') != self.runtime.config['issuer']: return 403, common, {'error': 'invalid_origin'}
                if headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                    return 400, common, {'error': 'invalid_request'}
                values = parse_qs(body.decode(), keep_blank_values=True)
                if set(values) != {'csrf'} or len(values['csrf']) != 1: raise ValueError()
                self.runtime.mcp_store.consume_ticket(values['csrf'][0], 'drive_start_csrf', owner)
                location = self.runtime.oauth.begin(self.runtime.settings['owner_email'])
                return 303, dict(common, Location=location), None
            return 405, common, {'error': 'method_not_allowed'}
        except (ValueError, UnicodeError, StateError): return 400, common, {'error': 'invalid_request'}
        except (DriveError, ArchiveFull): return 503, common, {'error': 'archive_unavailable'}

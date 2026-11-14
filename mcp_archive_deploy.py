"""Narrow local Drive proxy and archive deployment boundaries."""
import ipaddress
import re


def drive_proxy_config(relay_instance=None):
    lines = [
        'map $request_uri $lylahub_drive_uri {',
        '    ~^/api(/drive/.*)$ $1;',
        '    default $request_uri;',
        '}',
        'server {',
        '    listen 127.0.0.1:8094;',
        '    server_name _;',
        '    access_log off;',
        '    error_log /dev/null crit;',
        '    client_max_body_size 10m;',
        '    client_body_timeout 10s;',
        '    include /etc/nginx/snippets/lylahub-mcp-drive-auth.conf;',
        '    if ($http_x_lylahub_drive_ip !~ "^[0-9]{1,3}(\\.[0-9]{1,3}){3}$") { return 400; }',
    ]
    routes = [
        ('location = /token', 'oauth2.googleapis.com', 'POST', '/token'),
        ('location ~ "^/api/drive/v3/(about|files(/generateIds|/[A-Za-z0-9_-]{1,200})?)$"',
         'www.googleapis.com', 'GET POST PATCH', '$lylahub_drive_uri'),
        ('location ~ "^/upload/drive/v3/files(/[A-Za-z0-9_-]{1,200})?$"',
         'www.googleapis.com', 'POST PATCH PUT', '$lylahub_drive_uri'),
    ]
    for location, host, methods, uri in routes:
        lines += ['    ' + location + ' {',
                  '        limit_except ' + methods + ' { deny all; }',
                  '        proxy_pass https://$http_x_lylahub_drive_ip' + uri + ';',
                  '        proxy_set_header Host ' + host + ';',
                  '        proxy_ssl_server_name on;',
                  '        proxy_ssl_name ' + host + ';',
                  '        proxy_ssl_verify on;',
                  '        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;',
                  '        proxy_pass_request_headers off;',
                  '        proxy_set_header Authorization $http_authorization;',
                  '        proxy_set_header Content-Type $http_content_type;',
                  '        proxy_set_header Content-Length $http_content_length;',
                  '        proxy_set_header X-Upload-Content-Length $http_x_upload_content_length;',
                  '        proxy_set_header X-Upload-Content-Type $http_x_upload_content_type;',
                  '        proxy_set_header Accept-Encoding identity;',
                  '        proxy_request_buffering off;',
                  '        proxy_buffering off;',
                  '        proxy_max_temp_file_size 0;',
                  '        proxy_http_version 1.1;',
                  '        proxy_redirect off;',
                  '        proxy_connect_timeout 3s;',
                  '        proxy_read_timeout 10s;',
                  '        proxy_send_timeout 10s;',
                  '    }']
    if relay_instance is not None:
        if not isinstance(relay_instance, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', relay_instance):
            raise ValueError('archive_invalid_relay')
        lines += ['    location = /relay/' + relay_instance + ' {',
                  '        limit_except POST { deny all; }', '        client_max_body_size 256k;',
                  '        proxy_pass http://192.0.2.10:8081/webhooks/' + relay_instance + ';',
                  '        proxy_pass_request_headers off;', '        proxy_set_header Content-Type application/json;',
                  '        proxy_set_header Content-Length $http_content_length;',
                  '        proxy_request_buffering off;', '        proxy_buffering off;',
                  '        proxy_max_temp_file_size 0;', '        proxy_read_timeout 10s;', '    }']
    lines += ['    location / { return 404; }', '}', '']
    return '\n'.join(lines)


def _insert(text, start, end, block, anchor):
    if start in text or end in text:
        if text.count(start) != 1 or text.count(end) != 1: raise ValueError('archive_config_markers')
        previous = text[text.index(start):text.index(end) + len(end)]
        if previous != block: raise ValueError('archive_unexpected_config')
        return text
    if text.count(anchor) != 1: raise ValueError('archive_config_anchor')
    return text.replace(anchor, block + '\n    ' + anchor, 1)


def patch_archive_nginx(text, *, docker_subnet):
    subnet = ipaddress.ip_network(docker_subnet, strict=True)
    if subnet.version != 4 or not subnet.is_private or subnet.prefixlen < 16:
        raise ValueError('archive_invalid_docker_subnet')
    public = ['# BEGIN LYLAHUB DRIVE']
    for path in ('/drive/oauth/start', '/drive/oauth/callback', '/drive/status'):
        login = path != '/drive/oauth/callback'
        public += ['    location = ' + path + ' {',
                   '        auth_basic "LylaHub WhatsMiau";' if login else '        auth_basic off;']
        if login: public += ['        auth_basic_user_file /etc/nginx/exemplo.htpasswd;']
        public += ['        client_max_body_size 16k;', '        client_body_timeout 5s;',
                   '        proxy_pass http://127.0.0.1:8090;', '        proxy_set_header Host $host;',
                   '        proxy_set_header Authorization "";',
                   '        proxy_set_header X-LylaHub-User ' + ('$remote_user;' if login else '"";'),
                   '        proxy_set_header X-LylaHub-Archive-Login ' + ('"1";' if login else '"";'),
                   '        proxy_set_header X-LylaHub-Archive-Hook "";',
                   '        proxy_set_header X-LylaHub-Archive-Token "";',
                   '        proxy_set_header X-LylaHub-Panel "";',
                   '        proxy_set_header X-LylaHub-MCP-Login "";', '        proxy_read_timeout 75s;',
                   '        access_log off;', '        error_log /dev/null crit;',
                   '        add_header Cache-Control "no-store" always;',
                   '        add_header Referrer-Policy ' + ('same-origin' if login else 'no-referrer') + ' always;', '    }']
    public += ['    location ^~ /archive/ { return 404; }', '    # END LYLAHUB DRIVE']
    text = _insert(text, '# BEGIN LYLAHUB DRIVE', '# END LYLAHUB DRIVE', '\n'.join(public),
                   'location = / { return 302 /painel/; }')
    information = ['# BEGIN LYLAHUB APP INFO']
    for name in ('info', 'privacy'):
        information += ['    location = /drive/' + name + ' {', '        auth_basic off;',
            '        limit_except GET { deny all; }',
            '        alias /opt/projeto-exemplo/panel/static/drive-' + name + '.html;',
            '        default_type text/html;', '        charset utf-8;',
            '        add_header X-Content-Type-Options nosniff always;',
            '        add_header Referrer-Policy no-referrer always;',
            '        add_header Content-Security-Policy "default-src \'none\'; frame-ancestors \'none\'; base-uri \'none\'" always;',
            '    }']
    information += ['    # END LYLAHUB APP INFO']
    text = _insert(text, '# BEGIN LYLAHUB APP INFO', '# END LYLAHUB APP INFO', '\n'.join(information),
                   'location = / { return 302 /painel/; }')
    # A separate new path in the existing private listener. Existing bot route is untouched.
    hook = '\n'.join(['# BEGIN LYLAHUB ARCHIVE HOOK',
        '    location ~ "^/archive-webhooks/([A-Za-z0-9_-]{1,100})$" {',
        '        set $lylahub_archive_instance $1;',
        '        allow ' + str(subnet) + ';', '        allow 127.0.0.1;', '        deny all;',
        '        limit_except POST { deny all; }', '        client_max_body_size 256k;',
        '        include /etc/nginx/snippets/lylahub-mcp-archive-hook.conf;',
        '        proxy_set_header X-LylaHub-Archive-Hook "1";',
        '        proxy_set_header X-LylaHub-Archive-Login "";',
        '        proxy_set_header X-LylaHub-User "";', '        proxy_set_header Authorization "";',
        '        proxy_set_header Host whatsmiau.example.org;',
        '        proxy_pass http://127.0.0.1:8090/archive/events/$lylahub_archive_instance;',
        '        proxy_read_timeout 15s;', '        access_log off;', '        error_log /dev/null crit;',
        '    }', '    # END LYLAHUB ARCHIVE HOOK'])
    if text.count('listen 192.0.2.10:8081;') != 1: raise ValueError('archive_private_listener_missing')
    return _insert(text, '# BEGIN LYLAHUB ARCHIVE HOOK', '# END LYLAHUB ARCHIVE HOOK', hook,
                   'location ~ "^/webhooks/[A-Za-z0-9_-]{1,100}$" {')

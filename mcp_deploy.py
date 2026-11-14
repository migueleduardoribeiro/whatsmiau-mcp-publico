"""Narrow, idempotent config patches; never replace a gateway server block."""


def patch_mcp_nginx(text):
    start, end = '# BEGIN LYLAHUB MCP', '# END LYLAHUB MCP'
    if start in text or end in text:
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
            raise ValueError('Unexpected MCP configuration markers')
        # Never silently preserve an edited/unexpected security boundary.
        actual = text[text.index(start):text.index(end) + len(end)]
        current = nginx_locations()
        legacy = nginx_locations(include_icon=False)
        expected = (current,legacy,current.replace('Referrer-Policy same-origin always;', 'Referrer-Policy no-referrer always;'),
                    legacy.replace('Referrer-Policy same-origin always;', 'Referrer-Policy no-referrer always;'))
        if actual not in expected:
            raise ValueError('MCP configuration differs from expected contract')
        return text.replace(actual, current, 1)
    anchor = 'location = / { return 302 /painel/; }'
    if text.count(anchor) != 1:
        raise ValueError('Expected panel redirect anchor')
    position = text.index(anchor)
    server_start = text.rfind('server {', 0, position)
    if server_start < 0 or 'server_name whatsmiau.example.org;' not in text[server_start:position]:
        raise ValueError('Expected WhatsMiau HTTPS server')
    return text[:position] + nginx_locations() + '\n    ' + text[position:]


def nginx_locations(include_icon=True):
    paths = ('/mcp', '/oauth/authorize', '/oauth/token', '/oauth/revoke',
             '/.well-known/oauth-authorization-server', '/.well-known/oauth-protected-resource',
             '/.well-known/oauth-protected-resource/mcp')
    blocks = ['# BEGIN LYLAHUB MCP']
    for path in paths:
        login = path == '/oauth/authorize'
        lines = ['    location = ' + path + ' {']
        lines.extend(['        auth_basic "LylaHub WhatsMiau";',
                      '        auth_basic_user_file /etc/nginx/exemplo.htpasswd;'] if login else
                     ['        auth_basic off;'])
        lines.extend(['        client_max_body_size 32k;', '        client_body_timeout 5s;',
                      '        proxy_request_buffering on;', '        proxy_buffering on;',
                      '        proxy_pass http://127.0.0.1:8090;', '        proxy_read_timeout 75s;',
                      '        proxy_set_header Host $host;',
                      '        proxy_set_header Authorization ' + ('"";' if login else '$http_authorization;'),
                      '        proxy_set_header X-LylaHub-User ' + ('$remote_user;' if login else '"";'),
                      '        proxy_set_header X-LylaHub-MCP-Login ' + ('"1";' if login else '"";'),
                      '        proxy_set_header X-LylaHub-Panel "";',
                      '        access_log off;', '        add_header Cache-Control "no-store" always;',
                      '        add_header Referrer-Policy ' + ('same-origin' if login else 'no-referrer') + ' always;', '    }'])
        blocks.append('\n'.join(lines))
    if include_icon:
        blocks.append('''    location = /mcp-icon.png {
        auth_basic off;
        limit_except GET { deny all; }
        alias /opt/projeto-exemplo/panel/static/whatsmiau-icon.png;
        default_type image/png;
        add_header X-Content-Type-Options nosniff always;
        add_header Cache-Control "public, max-age=86400" always;
        access_log off;
    }''')
    blocks.append('    # END LYLAHUB MCP')
    return '\n'.join(blocks)


def patch_mcp_unit(text):
    if 'ExecStart=/usr/bin/python3 -B /opt/projeto-exemplo/panel/server.py' not in text or text.count('[Service]') != 1:
        raise ValueError('Unexpected panel unit')
    limits = [line for line in text.splitlines() if line.startswith('MemoryMax=')]
    if limits not in (['MemoryMax=96M'], ['MemoryMax=160M']):
        raise ValueError('Unexpected panel memory limit')
    text = text.replace('MemoryMax=96M\n', 'MemoryMax=160M\n', 1)
    lines = ('StateDirectory=lylahub-panel', 'StateDirectoryMode=0700',
             'EnvironmentFile=-/etc/projeto-exemplo/mcp.env')
    for line in lines:
        key = line.split('=', 1)[0] + '='
        matches = [candidate for candidate in text.splitlines() if candidate.startswith(key)]
        if matches and matches != [line]:
            raise ValueError('Unexpected MCP unit settings')
        if not matches:
            text = text.replace('[Service]\n', '[Service]\n' + line + '\n', 1)
    return text

"""Restricted Groq egress. Google/WhatsApp proxy configuration is independent."""


def groq_proxy_config(*, vision=False):
    text = '''server {
    listen 127.0.0.1:8095;
    server_name _;
    access_log off;
    error_log /dev/null crit;
    client_max_body_size 10256k;
    client_body_timeout 10s;
    include /etc/nginx/snippets/lylahub-mcp-groq-auth.conf;
    if ($http_x_lylahub_groq_ip !~ "^[0-9]{1,3}(\\.[0-9]{1,3}){3}$") { return 400; }
    if ($args != "") { return 400; }
    location = /openai/v1/audio/transcriptions {
        limit_except POST { deny all; }
        proxy_pass https://$http_x_lylahub_groq_ip/openai/v1/audio/transcriptions;
        proxy_set_header Host api.groq.com;
        proxy_ssl_server_name on;
        proxy_ssl_name api.groq.com;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Content-Type $http_content_type;
        proxy_set_header Content-Length $http_content_length;
        proxy_set_header Accept-Encoding identity;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_http_version 1.1;
        proxy_redirect off;
        proxy_connect_timeout 3s;
        proxy_read_timeout 45s;
        proxy_send_timeout 10s;
    }
    location / { return 404; }
}
'''
    if vision:
        route = text.split('    location = /openai/v1/audio/transcriptions {', 1)[1].split('    location / {', 1)[0]
        route = route.replace('proxy_set_header Host api.groq.com;',
                              'proxy_set_header Host api.groq.com;\n        proxy_set_header User-Agent LylaHub-MCP/1.0;')
        text = text.replace('    location / {', '    location = /openai/v1/chat/completions {' +
                            route.replace('/openai/v1/audio/transcriptions', '/openai/v1/chat/completions') +
                            '    location / {')
    return text

"""Read-only contract check; run on gateway, never prints credentials or URLs."""
import json
import hashlib
import hmac
import urllib.request
from pathlib import Path

key = next(line.split('=', 1)[1].strip() for line in Path('/opt/projeto-exemplo/whatsmiau/.env').read_text().splitlines() if line.startswith('API_KEY='))
def get(path):
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080' + path, headers={'apikey': key}), timeout=10) as r:
        return json.load(r)
def bridge(path, method='GET', body=None, user='runtime-check', origin='https://whatsmiau.example.org'):
    headers = {'X-LylaHub-Panel': '1', 'Content-Type': 'application/json', 'Origin': origin}
    if user:
        headers['X-LylaHub-User'] = user
    request = urllib.request.Request('http://127.0.0.1:8090' + path, headers=headers, method=method,
                                     data=None if body is None else json.dumps(body).encode())
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers
spec = get('/docs/swagger.json')
paths = spec['paths']
for path in ('/v1/webhook/find/{instance}', '/v1/webhook/set/{instance}', '/v1/instance/logout/{id}', '/v1/instance/delete/{id}'):
    print(path, list(paths.get(path, {})))
instances = get('/v1/instance/fetchInstances')
print('INSTANCE_CONFIG_HMAC', hmac.new(key.encode(), json.dumps(instances, sort_keys=True).encode(), hashlib.sha256).hexdigest())
for instance in instances:
    w = get('/v1/webhook/find/' + instance['id'])['webhook']
    print(json.dumps({'id': instance['id'], 'name': instance.get('instanceName', instance['id']), 'enabled': w.get('enabled'), 'base64': w.get('base64'), 'events': w.get('events'), 'headers_configured': bool(w.get('headers')), 'lylahub_gateway': w.get('url') == 'http://192.0.2.10:8081/webhooks/' + instance['id'], 'syncFullHistory': instance.get('syncFullHistory', False), 'token_fields': [k for k in instance if 'token' in k.lower() or 'apikey' in k.lower()]}))
    state = get('/v1/instance/connectionState/' + instance['id'])
    print('STATE', instance['id'], state.get('instance', {}).get('state', state.get('status')))
    for operation in ('webhook', 'details'):
        status, data, headers = bridge('/instances/' + instance['id'] + '/' + operation)
        assert status == 200 and 'no-store' in headers['Cache-Control']
        assert key not in json.dumps(data)
        if operation == 'webhook':
            assert data['capabilities'] == {'scope': 'instance', 'base64': True, 'headers': False}
        else:
            assert data['credential_scope'] == 'global'
print('BRIDGE_READS_AND_FILTERING_OK')
assert bridge('/credentials/reveal')[0] == 404
assert bridge('/credentials/reveal', 'POST', {'confirm': True}, user=None)[0] == 403
assert bridge('/credentials/reveal', 'POST', {'confirm': True}, origin='https://evil.invalid')[0] == 403
status, data, headers = bridge('/credentials/reveal', 'POST', {'confirm': True})
assert status == 200 and data == {'api_key': key, 'scope': 'global'} and 'no-store' in headers['Cache-Control']
print('EXPLICIT_REVEAL_AUTH_CSRF_NO_CACHE_OK')
for relative in ('static/index.html', 'static/app.js', 'static/app.css'):
    assert key not in Path('/opt/projeto-exemplo/panel', relative).read_text()
print('NO_CREDENTIALS_IN_STATIC_ASSETS')

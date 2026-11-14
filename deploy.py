"""Update the existing panel only; preserve all other gateway configuration."""
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from mcp_deploy import patch_mcp_nginx, patch_mcp_unit

RELEASE_FILES = ('server.py', 'static/index.html', 'static/app.js', 'static/app.css',
                 'static/drive-info.html', 'static/drive-privacy.html','static/whatsmiau-icon.png','static/whatsmiau-icon.svg',
                 'mcp_contracts.py', 'mcp_store.py', 'mcp_oauth.py', 'mcp_tools.py',
                 'mcp_files.py', 'mcp_media.py', 'mcp_protocol.py',
                 'mcp_archive_models.py', 'mcp_archive_store.py', 'mcp_archive_catalog.py',
                 'mcp_archive_media.py', 'mcp_archive_worker.py', 'mcp_archive_query.py',
                 'mcp_archive_runtime.py', 'mcp_archive_http.py', 'mcp_drive_oauth.py',
                 'mcp_drive_client.py', 'mcp_drive_transport.py', 'mcp_transcription.py',
                 'mcp_transcription_store.py', 'mcp_transcription_cache.py', 'mcp_audio_source.py', 'mcp_groq.py',
                 'mcp_vision.py', 'mcp_conversation_models.py','mcp_conversation_source.py',
                 'mcp_conversation_store.py','mcp_conversation_catalog.py','mcp_conversation_query.py',
                 'mcp_read_coordinator.py')


def patch_nginx(text):
    marker = 'location ^~ /painel/api/ {'
    if text.count(marker) != 1:
        raise ValueError('Expected exactly one panel API location')
    start = text.index(marker) + len(marker)
    end = text.index('}', start)
    block = text[start:end]
    if '{' in block or 'auth_basic_user_file /etc/nginx/exemplo.htpasswd;' not in block or 'proxy_pass http://127.0.0.1:8090/;' not in block:
        raise ValueError('Unexpected panel API configuration; no files changed')
    for directive in ('proxy_set_header X-LylaHub-User', 'proxy_set_header Authorization', 'access_log', 'add_header Cache-Control', 'add_header Referrer-Policy'):
        block = re.sub(r'^[ \t]*' + re.escape(directive) + r'[^\n;]*;[ \t]*\n?', '', block, flags=re.MULTILINE)
    block = block.rstrip() + '\n        proxy_set_header X-LylaHub-User $remote_user;\n        proxy_set_header Authorization "";\n        access_log off;\n        add_header Cache-Control "no-store" always;\n        add_header Referrer-Policy no-referrer always;\n    '
    return text[:start] + block + text[end:]


def run(*args):
    subprocess.run(args, check=True)


def install(stage, sha):
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid release SHA')
    # Tests run entirely against mocks before any production change.
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py'], cwd=stage, check=True)
    for source in (stage / name for name in RELEASE_FILES):
        if not source.is_file() or source.is_symlink():
            raise ValueError('Missing release file')
    for name in RELEASE_FILES:
        if name.endswith('.py'):
            compile((stage / name).read_text(), name, 'exec')
    nginx = Path('/etc/nginx/sites-available/lylahub')
    patched = patch_mcp_nginx(patch_nginx(nginx.read_text()))
    unit = Path('/etc/systemd/system/lylahub-panel.service')
    patched_unit = patch_mcp_unit(unit.read_text())
    panel = Path('/opt/projeto-exemplo/panel')
    backup = Path('/opt/projeto-exemplo/panel-backups') / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + sha[:12])
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    shutil.copytree(panel, backup / 'panel')
    shutil.copy2(nginx, backup / 'nginx.conf')
    shutil.copy2(unit, backup / 'panel.service')
    def replace(source, target):
        temporary = target.with_name(target.name + '.new')
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        temporary.replace(target)
    try:
        for relative in RELEASE_FILES:
            replace(stage / relative, panel / relative)
        nginx.write_text(patched)
        unit.write_text(patched_unit)
        run('nginx', '-t')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'lylahub-panel')
        for attempt in range(15):
            try:
                request = urllib.request.Request('http://127.0.0.1:8090/instances', headers={'X-LylaHub-Panel': '1', 'X-LylaHub-User': 'deployment-check'})
                with urllib.request.urlopen(request, timeout=10) as response:
                    if not isinstance(json.load(response).get('instances'), list):
                        raise ValueError('Invalid health response')
                break
            except (OSError, ValueError):
                if attempt == 14:
                    raise
                time.sleep(1)
        run('systemctl', 'reload', 'nginx')
        run('systemctl', 'is-active', 'lylahub-panel', 'nginx')
        (panel / 'RELEASE').write_text(sha + '\n')
        print('PANEL_UPDATED', sha, 'backup:', backup)
    except BaseException:
        for relative in RELEASE_FILES:
            if (backup / 'panel' / relative).is_file():
                replace(backup / 'panel' / relative, panel / relative)
            elif relative.endswith('.py') and (panel / relative).is_file():
                (panel / relative).unlink()
        shutil.copy2(backup / 'nginx.conf', nginx)
        shutil.copy2(backup / 'panel.service', unit)
        run('nginx', '-t')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'lylahub-panel')
        run('systemctl', 'reload', 'nginx')
        print('PANEL_ROLLED_BACK', file=sys.stderr)
        raise


if __name__ == '__main__':
    install(Path(sys.argv[1]).resolve(), sys.argv[2])

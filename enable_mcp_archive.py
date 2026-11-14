"""Prepare Drive routes locally; never grants OAuth or enables conversation capture."""
import datetime
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from enable_mcp_media import replace_private
from mcp_archive_deploy import patch_archive_nginx, drive_proxy_config
from mcp_drive_oauth import CALLBACK


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def enable_archive(root=Path('/'), runner=run, *, owner_email, docker_subnet):
    root = Path(root).resolve()
    if not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}', owner_email): raise ValueError('archive_invalid_owner')
    config_path = root / 'etc/lylahub-panel/mcp.json'
    credentials = root / 'etc/lylahub-panel/google-drive-client.json'
    site = root / 'etc/nginx/sites-available/lylahub'
    proxy = root / 'etc/nginx/conf.d/lylahub-mcp-drive.conf'
    proxy_auth = root / 'etc/nginx/snippets/lylahub-mcp-drive-auth.conf'
    hook_auth = root / 'etc/nginx/snippets/lylahub-mcp-archive-hook.conf'
    targets = (config_path, site, proxy, proxy_auth, hook_auth)
    if any(path.is_symlink() or not path.parent.resolve().is_relative_to(root) for path in (*targets, credentials)):
        raise ValueError('archive_invalid_path')
    originals = {path: (path.read_bytes(), path.stat()) if path.exists() else None for path in targets}
    config = json.loads(config_path.read_bytes())
    client = json.loads(credentials.read_bytes()).get('web', {})
    if (not config.get('enable_media') or not client.get('client_id') or not client.get('client_secret')
            or CALLBACK not in client.get('redirect_uris', [])):
        raise ValueError('archive_prerequisites_missing')
    settings = dict(config.get('archive') or {})
    settings.setdefault('capture_enabled', False)
    settings.setdefault('trusted_view_once_metadata', False)
    settings.setdefault('relay_previous', False)
    settings.setdefault('proxy_token', secrets.token_urlsafe(48))
    settings.setdefault('hook_token', secrets.token_urlsafe(48))
    for key in ('proxy_token', 'hook_token'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', settings[key]): raise ValueError('archive_invalid_token')
    settings.update(owner_email=owner_email, directory='/var/lib/projeto-exemplo/archive',
                    credentials_path='/etc/projeto-exemplo/google-drive-client.json')
    config['archive'] = settings
    site_content = patch_archive_nginx(site.read_text(), docker_subnet=docker_subnet)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = root / 'var/backups' / ('lylahub-mcp-archive-' + timestamp + '-' + secrets.token_hex(4))
    backup.mkdir(mode=0o700)
    for index, path in enumerate(targets):
        if originals[path]: replace_private(backup / str(index), originals[path][0], 0o600)
    reloaded = False
    try:
        replace_private(proxy_auth, ('if ($http_x_lylahub_drive_token != "' + settings['proxy_token'] + '") { return 403; }\n').encode(), 0o600)
        replace_private(hook_auth, ('proxy_set_header X-LylaHub-Archive-Token "' + settings['hook_token'] + '";\n').encode(), 0o600)
        replace_private(proxy, drive_proxy_config(config['instance_id'] if settings['relay_previous'] else None).encode(), 0o644)
        replace_private(site, site_content.encode(), originals[site][1].st_mode & 0o777, originals[site][1])
        replace_private(config_path, json.dumps(config, indent=2).encode() + b'\n', 0o640, originals[config_path][1])
        runner('nginx', '-t')
        reloaded = True
        runner('systemctl', 'reload', 'nginx')
        runner('systemctl', 'restart', 'lylahub-panel')
        runner('systemctl', 'is-active', 'nginx', 'lylahub-panel')
        return {'prepared': True, 'capture_enabled': settings['capture_enabled'], 'backup': str(backup)}
    except BaseException:
        for path, previous in originals.items():
            if previous: replace_private(path, previous[0], previous[1].st_mode & 0o777, previous[1])
            elif path.exists(): path.unlink()
        if reloaded:
            runner('nginx', '-t'); runner('systemctl', 'reload', 'nginx'); runner('systemctl', 'restart', 'lylahub-panel')
        raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner-email', required=True)
    parser.add_argument('--docker-subnet', required=True)
    args = parser.parse_args()
    if os.name == 'nt' or os.geteuid() != 0: raise SystemExit('Run as root on the WhatsMiau VM')
    print(json.dumps(enable_archive(owner_email=args.owner_email, docker_subnet=args.docker_subnet)))

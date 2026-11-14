"""Explicit, rollback-capable activation of the approved local media proxy.

Run as root only after the panel MCP configuration and release are installed.
It never changes the WhatsMiau container, webhooks, NAT, or OAuth state database.
"""
import datetime
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from mcp_media import proxy_config


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def replace_private(path, data, mode, metadata=None):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.new')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            if metadata:
                os.chown(temporary, metadata.st_uid, metadata.st_gid)
            temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def enable_media(root=Path('/'), runner=run):
    root = Path(root).resolve()
    config_path = root / 'etc/lylahub-panel/mcp.json'
    proxy = root / 'etc/nginx/conf.d/lylahub-mcp-files.conf'
    auth = root / 'etc/nginx/snippets/lylahub-mcp-fetch-auth.conf'
    targets = (config_path, proxy, auth)
    if not config_path.is_file() or any(path.is_symlink() for path in targets):
        raise ValueError('Expected protected MCP configuration, no symlinks')
    if any(not path.parent.resolve().is_relative_to(root) for path in targets):
        raise ValueError('Paths must remain under install root')
    originals = {path: (path.read_bytes(), path.stat()) if path.exists() else None for path in targets}
    config = json.loads(originals[config_path][0])
    if not isinstance(config, dict) or not config.get('instance_id') or not isinstance(config.get('clients'), dict):
        raise ValueError('MCP must be configured before enabling media')
    if root == Path('/') and originals[config_path][1].st_uid != 0:
        raise ValueError('MCP configuration must be root-owned')
    token = config.get('media_fetch_token') or secrets.token_urlsafe(48)
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
        raise ValueError('Invalid media proxy credential')
    config.update(enable_media=True, media_fetch_token=token)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = root / 'var/backups' / ('lylahub-mcp-media-' + timestamp + '-' + secrets.token_hex(4))
    backup.mkdir(mode=0o700)
    for index, path in enumerate(targets):
        if originals[path]:
            replace_private(backup / str(index), originals[path][0], 0o600)
    reloaded = False
    try:
        replace_private(auth, ('if ($http_x_lylahub_fetch_token != "' + token + '") { return 403; }\n').encode(), 0o600)
        replace_private(proxy, proxy_config().encode(), 0o644)
        replace_private(config_path, json.dumps(config, indent=2).encode() + b'\n', 0o640, originals[config_path][1])
        runner('nginx', '-t')
        # A reload command can succeed in changing state before reporting failure.
        reloaded = True
        runner('systemctl', 'reload', 'nginx')
        runner('systemctl', 'restart', 'lylahub-panel')
        runner('systemctl', 'is-active', 'nginx', 'lylahub-panel')
        return {'enabled': True, 'backup': str(backup)}
    except BaseException:
        for path, previous in originals.items():
            if previous:
                replace_private(path, previous[0], previous[1].st_mode & 0o777, previous[1])
            elif path.exists():
                path.unlink()
        if reloaded:
            runner('nginx', '-t')
            runner('systemctl', 'reload', 'nginx')
            runner('systemctl', 'restart', 'lylahub-panel')
        raise


if __name__ == '__main__':
    if os.name == 'nt' or os.geteuid() != 0:
        raise SystemExit('Run as root on the WhatsMiau VM')
    print(json.dumps(enable_media(), sort_keys=True))

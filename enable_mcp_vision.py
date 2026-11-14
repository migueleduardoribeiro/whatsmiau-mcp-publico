"""Explicit opt-in for image inference using the existing free Groq account."""
import datetime
import json
import os
import secrets
from pathlib import Path
from enable_mcp_media import replace_private, run
from mcp_transcription_deploy import groq_proxy_config


def enable_vision(root=Path('/'), runner=run, *, authorized_at, free_plan_verified_at):
    for value in (authorized_at, free_plan_verified_at):
        try: parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, AttributeError): raise ValueError('vision_requires_authorized_free_plan') from None
        if parsed.tzinfo is None: raise ValueError('vision_requires_authorized_free_plan')
    root = Path(root).resolve()
    config_path = root/'etc/lylahub-panel/mcp.json'
    proxy = root/'etc/nginx/conf.d/lylahub-mcp-groq.conf'
    targets = (config_path, proxy)
    if any(p.is_symlink() or not p.is_file() or not p.parent.resolve().is_relative_to(root) for p in targets):
        raise ValueError('vision_requires_existing_groq_configuration')
    originals = {p: (p.read_bytes(), p.stat()) for p in targets}
    config = json.loads(originals[config_path][0])
    settings = config.get('transcription') or {}
    if settings.get('enabled') is not True or not settings.get('key_path') or not settings.get('proxy_token'):
        raise ValueError('vision_requires_existing_groq_configuration')
    config['vision'] = {'enabled': True, 'authorized_at': authorized_at,
                        'free_plan_verified_at': free_plan_verified_at}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = root/'var/backups'/('lylahub-mcp-vision-' + stamp + '-' + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    for i, p in enumerate(targets): replace_private(backup/str(i), originals[p][0], 0o600)
    reloaded = False
    try:
        replace_private(proxy, groq_proxy_config(vision=True).encode(), 0o644, originals[proxy][1])
        replace_private(config_path, json.dumps(config, indent=2).encode()+b'\n', 0o640, originals[config_path][1])
        runner('nginx', '-t')
        reloaded = True
        runner('systemctl', 'reload', 'nginx')
        runner('systemctl', 'restart', 'lylahub-panel')
        runner('systemctl', 'is-active', 'nginx', 'lylahub-panel')
    except BaseException:
        for p, (data, metadata) in originals.items(): replace_private(p, data, metadata.st_mode & 0o777, metadata)
        if reloaded:
            runner('nginx', '-t'); runner('systemctl', 'reload', 'nginx'); runner('systemctl', 'restart', 'lylahub-panel')
        raise
    return {'enabled': True, 'backup': str(backup), 'rpm': 2, 'rpd': 30}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorized-at', required=True)
    parser.add_argument('--free-plan-verified-at', required=True)
    args = parser.parse_args()
    if os.name == 'nt' or os.geteuid() != 0: raise SystemExit('Run as root on the WhatsMiau VM')
    print(json.dumps(enable_vision(authorized_at=args.authorized_at, free_plan_verified_at=args.free_plan_verified_at)))

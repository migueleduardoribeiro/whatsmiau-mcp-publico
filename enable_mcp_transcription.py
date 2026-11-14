"""Prepare restricted egress with inference disabled; never replaces usage or OAuth."""
import datetime
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from enable_mcp_media import replace_private
from mcp_transcription_deploy import groq_proxy_config
from mcp_transcription_store import LIMITS


def run(*args):
    subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def enable_transcription(root=Path('/'),runner=run,*,free_plan_verified_at,effective_limits):
    root=Path(root).resolve()
    try: verified=datetime.datetime.fromisoformat(free_plan_verified_at.replace('Z','+00:00'))
    except (ValueError,AttributeError): raise ValueError('free_plan_unverified') from None
    if verified.tzinfo is None: raise ValueError('free_plan_unverified')
    if (set(effective_limits)!=set(LIMITS) or any(type(v) is not int or not 1<=v<=LIMITS[k]
                                               for k,v in effective_limits.items())):
        raise ValueError('invalid_transcription_limits')
    config_path=root/'etc/lylahub-panel/mcp.json'
    proxy=root/'etc/nginx/conf.d/lylahub-mcp-groq.conf'
    auth=root/'etc/nginx/snippets/lylahub-mcp-groq-auth.conf'
    targets=(config_path,proxy,auth)
    if any(p.is_symlink() or not p.parent.resolve().is_relative_to(root) for p in targets):
        raise ValueError('invalid_transcription_path')
    if not (root/'usr/bin/ffprobe').is_file(): raise ValueError('ffprobe_required')
    config=json.loads(config_path.read_bytes())
    if not isinstance(config.get('archive'),dict): raise ValueError('archive_required')
    settings=dict(config.get('transcription') or {})
    token=settings.get('proxy_token') or secrets.token_urlsafe(48)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}',token): raise ValueError('invalid_proxy_token')
    if token in (config.get('media_fetch_token'),config['archive'].get('proxy_token'),config['archive'].get('hook_token')):
        raise ValueError('independent_proxy_token_required')
    settings.update(enabled=False,key_path='/etc/projeto-exemplo/groq.key',proxy_token=token,
                    free_plan_verified_at=free_plan_verified_at,limits=dict(effective_limits))
    config['transcription']=settings
    originals={p:(p.read_bytes(),p.stat()) if p.exists() else None for p in targets}
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=root/'var/backups'/('lylahub-mcp-transcription-'+stamp+'-'+secrets.token_hex(4))
    backup.mkdir(mode=0o700)
    for index,p in enumerate(targets):
        if originals[p]: replace_private(backup/str(index),originals[p][0],0o600)
    reloaded=False
    try:
        replace_private(auth,('if ($http_x_lylahub_groq_token != "'+token+'") { return 403; }\n').encode(),0o600)
        replace_private(proxy,groq_proxy_config().encode(),0o644)
        replace_private(config_path,json.dumps(config,indent=2).encode()+b'\n',0o640,originals[config_path][1])
        runner('nginx','-t')
        reloaded=True
        runner('systemctl','reload','nginx')
        runner('systemctl','restart','lylahub-panel')
        runner('systemctl','is-active','nginx','lylahub-panel')
    except BaseException:
        for p,previous in originals.items():
            if previous: replace_private(p,previous[0],previous[1].st_mode&0o777,previous[1])
            elif p.exists(): p.unlink()
        if reloaded:
            runner('nginx','-t'); runner('systemctl','reload','nginx'); runner('systemctl','restart','lylahub-panel')
        raise
    return {'prepared':True,'enabled':False,'backup':str(backup)}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--free-plan-verified-at',required=True)
    args=parser.parse_args()
    if os.name=='nt' or os.geteuid()!=0: raise SystemExit('Run as root on the WhatsMiau VM')
    print(json.dumps(enable_transcription(free_plan_verified_at=args.free_plan_verified_at,effective_limits=LIMITS)))

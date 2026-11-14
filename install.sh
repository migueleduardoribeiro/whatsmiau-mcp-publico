#!/bin/bash
set -euo pipefail
stage="$1"
backup="/etc/nginx/sites-available/lylahub.before-panel.$(date +%Y%m%d%H%M%S)"
cp -p /etc/nginx/sites-available/lylahub "$backup"
trap 'cp -p "$backup" /etc/nginx/sites-available/lylahub; nginx -t && systemctl reload nginx' ERR
id lylahub-panel >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin lylahub-panel
install -d -m 755 /opt/projeto-exemplo/panel/static
install -m 644 "$stage/server.py" /opt/projeto-exemplo/panel/server.py
install -m 644 "$stage/static/"* /opt/projeto-exemplo/panel/static/
install -m 644 "$stage/lylahub-panel.service" /etc/systemd/system/lylahub-panel.service
systemd-analyze verify /etc/systemd/system/lylahub-panel.service
systemctl daemon-reload
systemctl enable --now lylahub-panel
systemctl restart lylahub-panel
for attempt in $(seq 1 15); do
    if curl --fail --silent -H 'X-LylaHub-Panel: 1' -H 'X-LylaHub-User: installation-check' http://127.0.0.1:8090/instances >/dev/null; then break; fi
    sleep 1
done
curl --fail --silent -H 'X-LylaHub-Panel: 1' -H 'X-LylaHub-User: installation-check' http://127.0.0.1:8090/instances >/dev/null
install -m 644 "$stage/nginx-https.conf" /etc/nginx/sites-available/lylahub
nginx -t
systemctl reload nginx
systemctl is-active lylahub-panel nginx
echo 'PANEL_INSTALLED'

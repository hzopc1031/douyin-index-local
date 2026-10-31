#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname)" = VM-0-3-ubuntu
project=/srv/work-ecs-projects/douyin-index-local
test -f "$project/desktop_gateway.py"
test -f "$project/.cloud-state/access.json"
cat >/etc/systemd/system/douyin-index.service <<'UNIT'
[Unit]
Description=Douyin index cloud desktop
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=work-tencent-manager
Group=work-tencent-manager
WorkingDirectory=/srv/work-ecs-projects/douyin-index-local
ExecStart=/bin/bash /srv/work-ecs-projects/douyin-index-local/tencent-start.sh
Restart=always
RestartSec=10
UMask=0077
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/work-ecs-projects/douyin-index-local
LimitCORE=0
TasksMax=1024
KillMode=mixed
TimeoutStopSec=12
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now douyin-index.service
systemctl --no-pager --full status douyin-index.service
echo 'HTTPS access credentials (keep private):'
cat "$project/.cloud-state/access.json"
echo
echo 'Certificate SHA256 fingerprint:'
openssl x509 -in "$project/.cloud-state/server.crt" -noout -fingerprint -sha256
echo 'If Tencent firewall blocks inbound TCP 8443, allow it for your access IP.'

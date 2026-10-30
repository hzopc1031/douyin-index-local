#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run this script from Tencent console with sudo.'; exit 1; }
test "$(hostname)" = VM-0-3-ubuntu || { echo 'Wrong host; stopped.'; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 xvfb fluxbox x11vnc novnc websockify fonts-noto-cjk ca-certificates openssl
echo 'System dependencies installed. No desktop port or service has been opened.'

#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
STATE_DIR="${INDEX_HUB_STATE_DIR:-$PWD/.cloud-state}"
LOG_DIR="${INDEX_HUB_LOG_DIR:-$PWD/logs}"
mkdir -p "$STATE_DIR" "$LOG_DIR"
chmod 750 "$LOG_DIR"
exec .venv/bin/python desktop_gateway.py >>"$LOG_DIR/gateway.stdout.log" 2>&1

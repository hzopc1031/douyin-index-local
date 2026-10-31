#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
STATE_DIR="${INDEX_HUB_STATE_DIR:-$PWD/.cloud-state}"
LOG_DIR="${INDEX_HUB_LOG_DIR:-$PWD/logs}"
mkdir -p "$STATE_DIR" "$LOG_DIR"
chmod 750 "$LOG_DIR"
bash ./tencent-vnc-check.sh "$STATE_DIR"
export HOME="$PWD/.cloud-home"
export XDG_RUNTIME_DIR="$HOME/run"
export DISPLAY=:107
export PYWEBVIEW_GUI=qt
export INDEX_HUB_ENABLED=1
export QT_API=pyside6
export QT_LOGGING_RULES="js=false"
export LD_LIBRARY_PATH="$PWD/.runtime/qt-libs/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$HOME" "$XDG_RUNTIME_DIR" "$STATE_DIR"
children=()
cleanup() { if (( ${#children[@]} )); then kill "${children[@]}" 2>/dev/null || true; wait "${children[@]}" 2>/dev/null || true; fi; }
trap cleanup EXIT TERM INT
Xvfb :107 -screen 0 1600x1000x24 -nolisten tcp -noreset >>"$LOG_DIR/xvfb.stdout.log" 2>&1 &
children+=($!)
for attempt in {1..50}; do
  test -S /tmp/.X11-unix/X107 && break
  sleep 0.1
done
fluxbox >>"$LOG_DIR/desktop.stdout.log" 2>&1 &
children+=($!)
x11vnc -display :107 -localhost -rfbport 5907 -forever -shared -rfbauth "$STATE_DIR/vnc.passwd" -quiet >>"$LOG_DIR/vnc.stdout.log" 2>&1 &
children+=($!)
.venv/bin/python desktop_gateway.py >>"$LOG_DIR/gateway.stdout.log" 2>&1 &
children+=($!)
.venv/bin/python app.py >>"$LOG_DIR/app.stdout.log" 2>&1 &
children+=($!)
wait -n "${children[@]}"

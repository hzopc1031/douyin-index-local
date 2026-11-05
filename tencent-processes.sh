#!/bin/bash
# Shared by the two desktop entry points. Keep each child tree in its own
# process group, including descendants whose original parent has already exited.
set +m
children=()
app_pid=
bus_pid=
bus_dir=

start_child() {
  setsid "$@" &
  children+=("$!")
}

# Sets vnc_auth_args. Only the exact value 0 skips the password file.
# Any other value, including unset or empty, keeps the current fail-closed check.
prepare_vnc_auth() {
  vnc_auth_args=()
  if [[ "${INDEX_HUB_VNC_AUTH:-1}" == 0 ]]; then
    printf '%s\n' 'WARNING VNC authentication disabled (INDEX_HUB_VNC_AUTH is 0); x11vnc listens on localhost without a password' >>"$LOG_DIR/desktop.log"
    vnc_auth_args=(-nopw)
    return 0
  fi
  bash ./tencent-vnc-check.sh "$STATE_DIR"
  vnc_auth_args=(-rfbauth "$STATE_DIR/vnc.passwd")
}

start_session_bus() {
  # Bash must remain systemd's MainPID: dbus-run-session does not forward TERM
  # to its command. Own the foreground daemon and its private filesystem socket.
  if ! command -v dbus-daemon >/dev/null; then
    echo 'Session D-Bus requires dbus-daemon; refusing to start the desktop.' >&2
    return 1
  fi
  # /tmp is writable with PrivateTmp + ProtectSystem=strict, and stays short
  # enough for a Unix socket even when the project/XDG runtime path is long.
  bus_dir=$(mktemp -d /tmp/dy-dbus.XXXXXXXX)
  coproc SESSION_BUS {
    exec setsid dbus-daemon --session --nofork --nopidfile \
      --address="unix:path=$bus_dir/bus" --print-address=1
  }
  bus_pid=$!
  children+=("$bus_pid")
  local address_fd=${SESSION_BUS[0]:-} input_fd=${SESSION_BUS[1]:-} address=
  # Bash may already have removed the coprocess descriptors if exec failed.
  if [[ -z "$address_fd" || -z "$input_fd" ]]; then
    echo 'Session D-Bus failed to publish its address within 5 seconds.' >&2
    return 1
  fi
  exec {input_fd}>&-
  if ! read -r -t 5 address <&"$address_fd"; then
    exec {address_fd}<&-
    echo 'Session D-Bus failed to publish its address within 5 seconds.' >&2
    return 1
  fi
  exec {address_fd}<&-
  if [[ "$address" != "unix:path=$bus_dir/bus" && "$address" != "unix:path=$bus_dir/bus,guid="* ]] ||
      [[ ! -S "$bus_dir/bus" ]] || ! kill -0 "$bus_pid" 2>/dev/null; then
    echo 'Session D-Bus did not start on its private socket.' >&2
    return 1
  fi
  export DBUS_SESSION_BUS_ADDRESS="$address"
  unset DBUS_SESSION_BUS_PID DBUS_SESSION_BUS_WINDOWID DBUS_STARTER_ADDRESS DBUS_STARTER_BUS_TYPE
}

wait_until_gone() {
  local ticks=$1 pid alive
  shift
  while (( ticks-- > 0 )); do
    alive=0
    for pid in "$@"; do
      if kill -0 -- "$pid" 2>/dev/null; then alive=1; fi
    done
    (( alive )) || return 0
    sleep 0.1
  done
  return 1
}

cleanup() {
  # A signal exits through EXIT exactly once; repeated signals cannot restart
  # the deadline or interrupt reaping. No new service children start here.
  trap - EXIT
  trap '' TERM INT
  local pid
  local groups=() desktop_children=()
  if [[ -n "$app_pid" ]]; then
    # Leave Xvfb, D-Bus and Chromium alive for Qt's profile flush / flock release.
    kill -TERM "$app_pid" 2>/dev/null || true
    wait_until_gone 60 "$app_pid" || true  # QtShutdown has a five-second deadline.
  fi
  for pid in "${children[@]}"; do
    [[ "$pid" == "$bus_pid" ]] && continue
    desktop_children+=("$pid")
    groups+=("-$pid")
  done
  if (( ${#desktop_children[@]} )); then
    # Include PIDs to cover TERM arriving before a just-forked child runs setsid.
    for pid in "${desktop_children[@]}"; do
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    wait_until_gone 20 "${groups[@]}" "${desktop_children[@]}" || true
    kill -KILL -- "${groups[@]}" "${desktop_children[@]}" 2>/dev/null || true
    wait "${desktop_children[@]}" 2>/dev/null || true
  fi
  # Keep D-Bus available through Qt's exit, including the group escalation above.
  # One final second for the bus keeps the total grace budget at 6 + 2 + 1 < 12s.
  if [[ -n "$bus_pid" ]]; then
    kill -TERM -- "-$bus_pid" 2>/dev/null || kill -TERM "$bus_pid" 2>/dev/null || true
    wait_until_gone 10 "-$bus_pid" "$bus_pid" || true
    kill -KILL -- "-$bus_pid" "$bus_pid" 2>/dev/null || true
    wait "$bus_pid" 2>/dev/null || true
  fi
  if [[ -n "$bus_dir" ]]; then
    rm -f -- "$bus_dir/bus"
    rmdir -- "$bus_dir"
  fi
}

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

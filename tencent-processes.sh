#!/bin/bash
# Shared by the two desktop entry points. Keep each child tree in its own
# process group, including descendants whose original parent has already exited.
set +m
children=()
app_pid=

start_child() {
  setsid "$@" &
  children+=("$!")
}

start_session_bus() {
  # Bash must remain systemd's MainPID: dbus-run-session does not forward TERM
  # to its command. Keep the same private session bus, owned by this supervisor.
  coproc SESSION_BUS { exec setsid dbus-daemon --session --nofork --print-address=1; }
  children+=("$SESSION_BUS_PID")
  local address_fd=${SESSION_BUS[0]}
  read -r DBUS_SESSION_BUS_ADDRESS <&"$address_fd"
  exec {address_fd}<&-
  export DBUS_SESSION_BUS_ADDRESS
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
  local groups=()
  if [[ -n "$app_pid" ]]; then
    # Leave Xvfb, D-Bus and Chromium alive for Qt's profile flush / flock release.
    kill -TERM "$app_pid" 2>/dev/null || true
    wait_until_gone 60 "$app_pid" || true  # QtShutdown has a five-second deadline.
  fi
  for pid in "${children[@]}"; do groups+=("-$pid"); done
  if (( ${#children[@]} )); then
    # Include PIDs to cover TERM arriving before a just-forked child runs setsid.
    for pid in "${children[@]}"; do
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    wait_until_gone 20 "${groups[@]}" "${children[@]}" || true
    kill -KILL -- "${groups[@]}" "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

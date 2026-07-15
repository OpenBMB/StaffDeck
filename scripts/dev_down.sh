#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.dev"

remove_legacy_launchctl_labels() {
  for prefix in com.StaffDeck.dev com.skill-agent-loop; do
    for name in app backend enterprise chat; do
      launchctl remove "$prefix.$name" >/dev/null 2>&1 || true
    done
  done
}

stop_pid_file() {
  local name="$1"
  local pid_file="$RUN_DIR/$name.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name was not started by scripts/dev_up.sh"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"

  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "$name pid file was stale"
    return 0
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name was not running"
    return 0
  fi

  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "Stopped $name ($pid)"
      return 0
    fi
    sleep 0.1
  done

  kill -9 "$pid" 2>/dev/null || true
  echo "Force-stopped $name ($pid)"
}

remove_legacy_launchctl_labels

for name in supervisor app backend enterprise chat; do
  stop_pid_file "$name"
done

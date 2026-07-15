#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.dev"

SINGLE_PORT="${SINGLE_PORT:-1}"
APP_PORT="${APP_PORT:-5173}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
ENTERPRISE_PORT="${ENTERPRISE_PORT:-5173}"

label_present() {
  local label="$1"
  launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1
}

print_service() {
  local name="$1"
  local pid_file="$RUN_DIR/$name.pid"

  if label_present "com.StaffDeck.dev.$name" || label_present "com.skill-agent-loop.$name"; then
    echo "  $name has legacy launchctl label; run scripts/dev_down.sh"
    return 0
  fi

  if [[ ! -f "$pid_file" ]]; then
    echo "  $name not started by scripts/dev_up.sh"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "  $name running ($pid)"
  else
    echo "  $name stale pid"
  fi
}

print_port() {
  local port="$1"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "  $port listening"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed 's/^/    /'
  else
    echo "  $port not listening"
  fi
}

echo "Processes:"
if [[ "$SINGLE_PORT" == "1" ]]; then
  for name in supervisor app; do
    print_service "$name"
  done
else
  for name in supervisor backend enterprise; do
    print_service "$name"
  done
fi
echo

echo "Ports:"
if [[ "$SINGLE_PORT" == "1" ]]; then
  print_port "$APP_PORT"
else
  print_port "$BACKEND_PORT"
  print_port "$ENTERPRISE_PORT"
fi
echo

echo "Health:"
if [[ "$SINGLE_PORT" == "1" ]]; then
  curl -fsS "http://127.0.0.1:$APP_PORT/api/health" || true
  echo
  curl -fsS "http://127.0.0.1:$APP_PORT/enterprise/dashboard" >/dev/null && echo "enterprise ok" || echo "enterprise unavailable"
  curl -fsS "http://127.0.0.1:$APP_PORT/chat/" >/dev/null && echo "chat ok" || echo "chat unavailable"
else
  curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" || true
  echo
  curl -fsSI "http://127.0.0.1:$ENTERPRISE_PORT/enterprise/dashboard" >/dev/null && echo "enterprise ok" || echo "enterprise unavailable"
  curl -fsSI "http://127.0.0.1:$ENTERPRISE_PORT/chat/" >/dev/null && echo "chat ok" || echo "chat unavailable"
fi

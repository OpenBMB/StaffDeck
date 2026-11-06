#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$REPO/packaging/out/StaffDeck.app}"
EXECUTABLE="$APP/Contents/MacOS/staffdeck"
PORT="${STAFFDECK_SMOKE_PORT:-}"
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/staffdeck-macos-smoke.XXXXXX")"
LAUNCHER_LOG="$DATA_DIR/launcher.log"
HEALTH_RESPONSE="$DATA_DIR/health.json"
PID=""

dump_logs() {
  echo "==> macOS packaged-app smoke logs" >&2
  if [ -s "$LAUNCHER_LOG" ]; then
    tail -n 200 "$LAUNCHER_LOG" >&2
  fi
  if [ -d "$DATA_DIR/logs" ]; then
    while IFS= read -r log_file; do
      echo "--- $log_file" >&2
      tail -n 200 "$log_file" >&2
    done < <(find "$DATA_DIR/logs" -type f -print)
  fi
}

cleanup() {
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 50); do
      if ! kill -0 "$PID" 2>/dev/null; then break; fi
      sleep 0.2
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "Packaged-app test process exceeded the shutdown grace period; stopping it" >&2
      kill -KILL "$PID" 2>/dev/null || true
    fi
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

if [ ! -x "$EXECUTABLE" ]; then
  echo "Packaged macOS executable not found: $EXECUTABLE" >&2
  exit 1
fi

if [ -z "$PORT" ]; then
  PORT="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
fi

echo "==> Smoke-testing packaged macOS app ($(uname -m))"
env \
  STAFFDECK_HEADLESS=1 \
  ULTRARAG_DATA_DIR="$DATA_DIR" \
  ULTRARAG_PORT="$PORT" \
  ULTRARAG_PORT_RANGE_START="$PORT" \
  ULTRARAG_PORT_RANGE_END="$PORT" \
  "$EXECUTABLE" >"$LAUNCHER_LOG" 2>&1 &
PID=$!

for _ in $(seq 1 90); do
  if ! kill -0 "$PID" 2>/dev/null; then
    wait "$PID" || true
    echo "Packaged macOS app exited before becoming healthy" >&2
    dump_logs
    exit 1
  fi
  if curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$PORT/api/health" >"$HEALTH_RESPONSE" 2>/dev/null; then
    if grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' "$HEALTH_RESPONSE"; then
      echo "OK: Packaged macOS app health check passed ($(uname -m))"
      exit 0
    fi
  fi
  sleep 1
done

echo "Packaged macOS app health check timed out" >&2
dump_logs
exit 1

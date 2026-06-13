#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd "$APP_DIR/../voice-router-pipecat" && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"
cd "$APP_DIR"
PID_FILE="$PWD/voice-router.pid"
LOG_FILE="$PWD/voice-router.log"

if [ ! -x "$PYTHON" ]; then
  echo "missing venv python: $PYTHON" >&2
  exit 1
fi

if [ -s "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE")"
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "already running pid $old_pid"
    "$ROUTER_DIR/voice_status.py" enabled on
    exit 0
  fi
  rm -f "$PID_FILE"
fi

export VOICE_ROUTER_LLM_BASE_URL="${VOICE_ROUTER_LLM_BASE_URL:-}"
export VOICE_ROUTER_LLM_MODEL="${VOICE_ROUTER_LLM_MODEL:-}"
export VOICE_ROUTER_TTS_CMD="${VOICE_ROUTER_TTS_CMD:-}"
export VOICE_ROUTER_INPUT_DEVICE_INDEX="${VOICE_ROUTER_INPUT_DEVICE_INDEX:-1}"

nohup "$PYTHON" app.py >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"
sleep 0.5
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  "$ROUTER_DIR/voice_status.py" enabled off
  echo "failed to start voice router; see $LOG_FILE" >&2
  exit 1
fi

"$ROUTER_DIR/voice_status.py" enabled on
"$ROUTER_DIR/voice_status.py" mode idle
echo "started pid $pid"

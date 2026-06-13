#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd "$APP_DIR/../voice-router-pipecat" && pwd)"
cd "$APP_DIR"
PID_FILE="$PWD/voice-router.pid"
LOG_FILE="$PWD/voice-router.log"
if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "already running pid $(cat "$PID_FILE")"
  "$ROUTER_DIR/voice_status.py" enabled on
  exit 0
fi
source .venv/bin/activate
export VOICE_ROUTER_LLM_BASE_URL="${VOICE_ROUTER_LLM_BASE_URL:-}"
export VOICE_ROUTER_LLM_MODEL="${VOICE_ROUTER_LLM_MODEL:-}"
export VOICE_ROUTER_TTS_CMD="${VOICE_ROUTER_TTS_CMD:-}"
export VOICE_ROUTER_INPUT_DEVICE_INDEX="${VOICE_ROUTER_INPUT_DEVICE_INDEX:-1}"
nohup python app.py >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
"$ROUTER_DIR/voice_status.py" enabled on
"$ROUTER_DIR/voice_status.py" mode idle
echo "started pid $!"

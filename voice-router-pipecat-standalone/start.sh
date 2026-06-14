#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd "$APP_DIR/../voice-router-pipecat" && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "missing venv python: $PYTHON" >&2
  exit 1
fi

if systemctl --user is-active --quiet pipecat-voice.service; then
  echo "already running (pipecat-voice.service)"
  "$ROUTER_DIR/voice_status.py" enabled on
  exit 0
fi

systemctl --user start pig-io.service
if ! systemctl --user start pipecat-voice.service; then
  echo "failed to start pipecat-voice.service; see: journalctl --user -u pipecat-voice -n 40" >&2
  exit 1
fi

echo "pipecat-voice.service: $(systemctl --user is-active pipecat-voice.service)"

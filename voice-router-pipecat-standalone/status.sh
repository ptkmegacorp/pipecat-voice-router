#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd "$APP_DIR/../voice-router-pipecat" && pwd)"

state="$(systemctl --user is-active pipecat-voice.service 2>/dev/null || echo inactive)"
echo "pipecat-voice.service: $state"
if systemctl --user is-enabled pipecat-voice.service >/dev/null 2>&1; then
  echo "boot: enabled"
else
  echo "boot: on-demand (not enabled at login)"
fi

"$ROUTER_DIR/voice_status.py" show
if pactl list short sources 2>/dev/null | grep -q $'\tpipecat_aec_source\t'; then
  echo "aec source: pipecat_aec_source"
else
  echo "aec source: unavailable"
fi
if pactl list short sinks 2>/dev/null | grep -q $'\tpipecat_aec_sink\t'; then
  echo "aec sink: pipecat_aec_sink"
else
  echo "aec sink: unavailable"
fi
for p in 8091 8090 8088 8080; do
  models=$(curl -s --max-time 1 "http://127.0.0.1:$p/v1/models" 2>/dev/null || true)
  if [ -n "$models" ]; then
    echo "llama-server: http://127.0.0.1:$p/v1"
    echo "$models" | python3 -c 'import json,sys; d=json.load(sys.stdin); ms=d.get("data") or d.get("models") or []; print("model:", (ms[0].get("id") or ms[0].get("model") or ms[0].get("name")) if ms else "unknown")' 2>/dev/null || true
    break
  fi
done

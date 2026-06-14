# Standalone Pipecat Voice Router

This is the standalone Pipecat app for voice control.

It is separate from the Pig extension. It uses Pipecat as the framework:

```text
local microphone
→ Pipecat LocalAudioTransport
→ Silero VAD
→ Moonshine STT
→ exact + fuzzy router
→ i3/browser action OR local LLM fallback
```

## Location

```text
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone
/home/bot/pipecat-voice-router/voice-router-pipecat
```

GitHub: https://github.com/ptkmegacorp/pipecat-voice-router

Pipecat source checkout:

```text
/home/bot/pipecat
```

Python venv:

```text
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/.venv
```

## Start / stop

```bash
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/start.sh
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/stop.sh
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/status.sh
```

## What depends on what

**pipecat does not need the overlay.** It talks to pig-io over HTTP.

| Service | Requires |
|---------|----------|
| **llama-server** | _(Gemma on :8091 — used by pig-io; pipecat fallback only if pig-io down)_ |
| **pig-io** | llama-server |
| **pipecat** (this app) | pig-io on :8765 for `ask_pig` / LLM fallback routes; PipeWire mic |
| **overlay** | pig-io only _(optional display — hide/show, not required for voice)_ |

**Start order:**

```bash
# 1. llama-server (:8091)

# 2. pig-io (:8765) — pipecat POSTs /ask here
cd ~/pig-io && ./stop.sh && ./start.sh

# 3. pipecat (this)
cd ~/pipecat-voice-router/voice-router-pipecat-standalone && ./stop.sh && ./start.sh
```

**Verify pipecat → pig-io path** (with both running):

```bash
curl -sf -X POST http://127.0.0.1:8765/ask \
  -H 'content-type: application/json' \
  -d '{"text":"test","source":"pipecat_voice"}'
```

Then speak — log should show `heard:` → `pig-io accepted`.

Rofi menu:

```text
Mod+Shift+v
```

Options:

- start standalone pipecat voice
- stop standalone pipecat voice
- restart standalone pipecat voice
- show status
- tail log

## Logs

```text
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/voice-router.log
```

## Routed commands

Uses route config from:

```text
/home/bot/pipecat-voice-router/voice-router-pipecat/router_config.json
```

Current direct routes:

- `scroll down`, `go down`, `page down`, `move down`
- `scroll up`, `go up`, `page up`, `move up`
- `make full screen`, `make fullscreen`, `fullscreen`, `full screen`, `toggle full screen`, `toggle fullscreen`
- `exit fullscreen`, `exit full screen`, `leave fullscreen`, `leave full screen`, `disable fullscreen`, `disable full screen`
- `open youtube and search for ...`
- `list all routed commands`

Anything else goes to the currently running local OpenAI-compatible llama-server endpoint.

Auto-discovery order:

```text
http://127.0.0.1:8091/v1  # Pig/local Gemma default on this machine
http://127.0.0.1:8090/v1  # Pi audio baseline
http://127.0.0.1:8088/v1  # Qwen text recipe
http://127.0.0.1:8080/v1
```

The app reads `/v1/models` and uses the first reported model id, so it follows whatever local llama-server is actually running.

Override if needed:

```bash
export VOICE_ROUTER_LLM_BASE_URL=http://127.0.0.1:8090/v1
export VOICE_ROUTER_LLM_MODEL=your-model-name
```

## TTS

By default, TTS command is blank. The app prints LLM answers to the log/stdout.

To enable simple command-line TTS:

```bash
export VOICE_ROUTER_TTS_CMD=spd-say
# or
export VOICE_ROUTER_TTS_CMD=espeak
```

Then start the app.

## i3bar status

The app updates:

```text
~/.cache/pipecat-voice/status.json
```

through:

```text
/home/bot/pipecat-voice-router/voice-router-pipecat/voice_status.py
```

So i3bar shows:

- `🎙 pipecat on`
- `🎙 hearing`
- `🎙 thinking`
- `🔊 speaking`
- `🎙 error`

## Notes

Pipecat is a framework. This directory is the actual standalone app built with Pipecat.

If microphone capture fails, check devices:

```bash
arecord -l
pactl list short sources
```

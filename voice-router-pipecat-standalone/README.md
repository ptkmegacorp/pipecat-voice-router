# Standalone Pipecat Voice Router

This is the standalone Pipecat app for voice control.

E2E routing map: [ROUTING.md](../../pig-io/ROUTING.md).

It is separate from the Pig extension. It uses Pipecat as the framework:

```text
local microphone
→ Pipecat LocalAudioTransport
→ Silero VAD
→ Moonshine STT
→ exact + fuzzy router
→ i3/overlay action OR Pig LLM fallback (Firefox/browser requests use fallback)
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

## Service stack (systemd)

```text
llama-server.service          always on
    └── pig-io.service        always on
            └── pipecat-voice.service   this app — on-demand only
```

| Service | Boot | Managed by |
|---------|------|------------|
| **llama-server** | enabled | `systemctl --user` |
| **pig-io** | enabled | `systemctl --user` |
| **pipecat** (this app) | **on-demand** | `systemctl --user start pipecat-voice` |
| **overlay** | i3 login | `overlay.sh` (GUI, not systemd) |

**pipecat does not need the overlay.** It talks to pig-io over HTTP.

## Start / stop

On-demand via rofi (`Mod+Shift+v`) or wrappers:

```bash
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/start.sh
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/stop.sh
/home/bot/pipecat-voice-router/voice-router-pipecat-standalone/status.sh
```

Equivalent systemd:

```bash
systemctl --user start pipecat-voice    # also ensures pig-io is up
systemctl --user stop pipecat-voice
systemctl --user status pipecat-voice
```

Pipecat is **not** `enable`d at boot. pig-io is.

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

- start / stop / restart standalone pipecat voice
- show status
- tail log (`journalctl --user -u pipecat-voice -f`)

## Logs

```bash
journalctl --user -u pipecat-voice -f
journalctl --user -u pig-io -f
```

Legacy file (no longer written): `voice-router.log`

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
- `open pig` / `focus pig` / `close pig` (overlay)
- `close youtube` (mpv)
- `list all routed commands`

Anything else — including Firefox, browser, and YouTube search requests — goes to Pig via pig-io (`/ask`) or the local OpenAI-compatible llama-server fallback.

Auto-discovery order:

```text
http://127.0.0.1:8091/v1  # Pig default (Gemma 4 12B QAT + MTP)
http://127.0.0.1:8092/v1  # LiteResearcher
http://127.0.0.1:8088/v1  # Qwen text recipe
http://127.0.0.1:8080/v1
```

The app reads `/v1/models` and uses the first reported model id, so it follows whatever local llama-server is actually running.

Override if needed:

```bash
export VOICE_ROUTER_LLM_BASE_URL=http://127.0.0.1:8091/v1
export VOICE_ROUTER_LLM_MODEL=gemma-4-12b-it-qat-mtp-local
```

Set overrides in `~/.config/systemd/user/pipecat-voice.service.d/override.conf` for persistence.

## TTS

By default, TTS command is blank. The app prints LLM answers to the log/stdout.

To enable simple command-line TTS:

```bash
export VOICE_ROUTER_TTS_CMD=spd-say
# or
export VOICE_ROUTER_TTS_CMD=espeak
```

Then restart: `systemctl --user restart pipecat-voice`

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

## Unit file

Install into `~/.config/systemd/user/`:

```bash
ln -sf ~/pipecat-voice-router/systemd/pipecat-voice.service ~/.config/systemd/user/
systemctl --user daemon-reload
# on-demand only — do not enable at boot:
# systemctl --user start pipecat-voice
```

Source copy in this repo:

```text
systemd/pipecat-voice.service
```

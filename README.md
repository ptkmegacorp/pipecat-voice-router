# Pipecat Voice Router

Local voice control for i3/Linux: mic → VAD → Moonshine STT → routed commands or local LLM fallback.

E2E routing map: [ROUTING.md](../pig-io/ROUTING.md).

## Layout

```text
pipecat-voice-router/
├── voice-router-pipecat/           # routing config, shared routing logic, i3bar status
└── voice-router-pipecat-standalone/ # Pipecat app, venv, start/stop scripts
```

## Pipeline

```text
local microphone
→ Pipecat LocalAudioTransport
→ Silero VAD
→ Moonshine STT
→ exact + fuzzy router
→ i3/overlay action OR Pig LLM fallback (Firefox/browser requests use fallback)
```

## Setup

Requires a local [Pipecat](https://github.com/pipecat-ai/pipecat) checkout and Python venv in `voice-router-pipecat-standalone/.venv`.

Example:

```bash
cd voice-router-pipecat-standalone
python3 -m venv .venv
source .venv/bin/activate
pip install -e /path/to/pipecat[moonshine]
pip install requests loguru
```

## Run

pig-io runs always-on via systemd. Pipecat is on-demand.

```bash
# pig-io (enabled at boot)
systemctl --user status pig-io

# pipecat (start when you want voice)
systemctl --user start pipecat-voice
# or wrappers:
voice-router-pipecat-standalone/start.sh
voice-router-pipecat-standalone/stop.sh
voice-router-pipecat-standalone/status.sh
```

Logs: `journalctl --user -u pipecat-voice -f`

Rofi menu (if configured): `Mod+Shift+v`

## Routed commands

See `voice-router-pipecat/router_config.json`. Examples:

- scroll up/down
- make / exit fullscreen
- focus / open / close pig-io overlay
- close youtube (mpv)
- list all routed commands
- anything else (including Firefox/browser/YouTube search) → Pig via pig-io or local llama-server

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOICE_ROUTER_VAD_STOP_SECS` | `0.9` | Silence before end-of-utterance |
| `VOICE_ROUTER_MOONSHINE_MODEL` | `tiny-streaming` | Moonshine STT model |
| `VOICE_ROUTER_FUZZY_THRESHOLD` | `85` | Fuzzy route match score |
| `VOICE_ROUTER_LLM_MAX_TOKENS` | `64` | LLM fallback response length |
| `VOICE_ROUTER_INPUT_DEVICE_MATCH` | `USB Composite Device,USB Audio` | Substrings to find mic (survives reboot) |
| `VOICE_ROUTER_INPUT_DEVICE_EXCLUDE` | `hdmi,nvidia,monitor,spdif` | Skip output/HDMI pseudo-devices |
| `VOICE_ROUTER_INPUT_DEVICE_INDEX` | _(empty)_ | Optional numeric override only |
| `VOICE_ROUTER_TTS_CMD` | _(empty)_ | e.g. `spd-say` for spoken replies |

## i3bar status

Written to `~/.cache/pipecat-voice/status.json` via `voice-router-pipecat/voice_status.py`.

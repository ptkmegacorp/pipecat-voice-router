# Pipecat Voice Router

Local voice control for i3/Linux: standalone mic routing to Pig/Pig-IO, plus universal push-to-talk transcript paste.

E2E routing map: [ROUTING.md](../pig-io/ROUTING.md).

## Layout

```text
pipecat-voice-router/
├── voice-router-pipecat/           # routing config, shared routing logic, i3bar status
└── voice-router-pipecat-standalone/ # Pipecat app, venv, start/stop scripts
```

## Pipelines

Pig/Pig-IO voice routing:

```text
Kokoro playback → PipeWire WebRTC AEC sink → HDMI speakers
USB microphone → PipeWire WebRTC AEC source → Pipecat
→ Silero VAD
→ Moonshine STT
→ Pipecat UserTurnProcessor + local Smart Turn v3
→ exact + fuzzy router
→ i3/overlay action OR Pig LLM fallback (Firefox/browser requests use fallback)
```

Universal paste mode:

```text
Ctrl+Space
→ record focused-desktop utterance
→ Ctrl+Space again
→ transcribe locally
→ clipboard paste transcript into focused input
→ restore previous clipboard
```

## Setup

Requires a local [Pipecat](https://github.com/pipecat-ai/pipecat) checkout, Python venv in `voice-router-pipecat-standalone/.venv`, and PipeWire-Pulse tools (`pactl`, `parec`) with WebRTC echo cancellation (`libpipewire-module-echo-cancel`, `libspa-aec-webrtc`).

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

## Turn management

The live router uses Pipecat's standard `UserTurnProcessor` with local Smart Turn v3. Raw VAD starts audio collection without immediately interrupting TTS. Accepted barge-in broadcasts a standard Pipecat interruption, while routing waits for `UserStoppedSpeakingFrame` so incomplete pauses can continue as one semantic turn.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOICE_ROUTER_VAD_STOP_SECS` | `0.9` | Silence before end-of-utterance |
| `VOICE_ROUTER_VAD_MIN_VOLUME` | `0.006` | Reject low-level AEC residue before Moonshine to prevent hallucinated utterances |
| `VOICE_ROUTER_BARGE_MIN_VAD_SECS` | `0.55` | Minimum energy-qualified speech span for arbitrary barge-in; explicit stop/wait commands bypass it |
| `VOICE_ROUTER_BARGE_MIN_RMS` | `0.04` | Normalized mic RMS required before TTS pauses; monitored continuously even when TTS residue opened VAD first |
| `FRONTIER_THINKING_REPEAT` | `0` | Speak one thinking cue; set to `1` to restore the repeating cadence |
| `VOICE_ROUTER_MOONSHINE_MODEL` | `small-streaming` | Moonshine STT model; improves accuracy over tiny-streaming |
| `VOICE_ROUTER_FUZZY_THRESHOLD` | `85` | Fuzzy route match score |
| `VOICE_ROUTER_LLM_MAX_TOKENS` | `64` | LLM fallback response length |
| `VOICE_ROUTER_AEC_SOURCE_MASTER` | first physical USB source | Optional PipeWire source override |
| `VOICE_ROUTER_AEC_SINK_MASTER` | first HDMI sink | Optional PipeWire sink override |
| `VOICE_ROUTER_AEC_SOURCE_NAME` | `pipecat_aec_source` | Clean virtual microphone source |
| `VOICE_ROUTER_AEC_SINK_NAME` | `pipecat_aec_sink` | Referenced Kokoro playback sink |
| `VOICE_ROUTER_TTS_CHUNK_CHARS` | `180` | Maximum streamed text before an unfinished sentence is queued for Kokoro |
| `VOICE_ROUTER_TTS_PAUSE_SECONDS` | `0.65` | Maximum initial buffering interval for a usable speech chunk |
| `VOICE_ROUTER_KOKORO_PYTHON` | `/home/bot/doc-tts/.venv/bin/python` | Python used by the persistent Kokoro-82M worker |
| `VOICE_ROUTER_TTS_SPEED` | `1.1` in the systemd unit | Kokoro speaking-rate multiplier |
| `VOICE_ROUTER_TTS_KEEP_LEADING_MS` | `40` | Low-energy margin retained before each synthesized chunk |
| `VOICE_ROUTER_TTS_KEEP_TRAILING_MS` | `100` | Low-energy margin retained after each synthesized chunk |

## i3bar status

Written to `~/.cache/pipecat-voice/status.json` via `voice-router-pipecat/voice_status.py`.

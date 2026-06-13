# i3bar Pipecat Voice Status

Current desktop uses i3 + i3bar with custom status script:

```text
/home/bot/.config/i3/status.sh
```

I added a voice status block that reads:

```text
~/.cache/pipecat-voice/status.json
```

Helper script:

```text
/home/bot/voice-router-pipecat/voice_status.py
```

## Status states

The bar can show:

- `voice: off` — Pipecat/voice router disabled
- `🎙 pipecat on` — enabled but not currently hearing speech
- `🎙 hearing` — VAD/STT says speech is currently active
- `🎙 thinking` — utterance received, model/tool work running
- `🔊 speaking` — TTS is speaking
- `🎙 error` — voice pipeline error

## Rofi control

Added a rofi menu script:

```text
/home/bot/.config/i3/bin/rofi-pipecat-voice.sh
```

It is available in rofi/drun as:

```text
Pipecat Voice Control
```

It also has an i3 keybinding:

```text
Mod+Shift+v
```

Menu options:

- toggle Pipecat voice on/off
- turn Pipecat voice on
- turn Pipecat voice off
- set mode idle/listening/thinking/speaking/error
- show status

Desktop entry:

```text
/home/bot/.local/share/applications/pipecat-voice-rofi.desktop
```

## Commands

```bash
/home/bot/voice-router-pipecat/voice_status.py enabled on
/home/bot/voice-router-pipecat/voice_status.py enabled off
/home/bot/voice-router-pipecat/voice_status.py hearing on
/home/bot/voice-router-pipecat/voice_status.py hearing off
/home/bot/voice-router-pipecat/voice_status.py mode idle
/home/bot/voice-router-pipecat/voice_status.py mode listening
/home/bot/voice-router-pipecat/voice_status.py mode thinking
/home/bot/voice-router-pipecat/voice_status.py mode speaking
/home/bot/voice-router-pipecat/voice_status.py mode error
/home/bot/voice-router-pipecat/voice_status.py show
```

## Current wiring

This is now wired through the existing Pig voice extension:

```text
/home/bot/projects/pi-voice-vad-gemma/src/index.ts
```

The extension already uses local VAD + Moonshine/Whisper STT. It now:

- polls the rofi/i3bar state file once per second
- starts continuous VAD when rofi sets `enabled: true`
- stops continuous VAD when rofi sets `enabled: false`
- updates i3bar on speech start/stop, thinking, speaking, and errors
- runs deterministic direct routed commands before sending fallback text to Pig/main LLM

Important: restart/open a new Pig session after code changes so the updated extension is loaded.

Runtime events:

```text
rofi enable on                 -> extension starts continuous VAD
rofi enable off                -> extension stops continuous VAD
VAD speech start               -> hearing on, mode listening
VAD speech stop                -> hearing off
final transcript committed     -> mode thinking
routed silent command done     -> mode idle
TTS start                      -> mode speaking
TTS stop                       -> mode idle/off
error                          -> mode error
session shutdown               -> enabled off
```

Python calls can directly import or shell out. Shell-out version:

```python
import subprocess

STATUS = "/home/bot/voice-router-pipecat/voice_status.py"

def set_voice(*args):
    subprocess.run([STATUS, *args], check=False)

set_voice("enabled", "on")
set_voice("hearing", "on")
set_voice("hearing", "off")
set_voice("mode", "thinking")
set_voice("mode", "speaking")
set_voice("mode", "idle")
```

## Why this is the best first approach

Use the existing i3bar status line instead of adding a new GUI overlay.

Advantages:

- already running
- always visible
- no extra window management
- easy to update from any process
- works with Pipecat, LiveKit, or a custom script
- simple file-based state means no server/socket required

Later, if we want a larger realtime waveform, add a small floating window. But for now the i3bar indicator is enough for:

```text
is voice enabled?
is it hearing me right now?
is it thinking/speaking?
```

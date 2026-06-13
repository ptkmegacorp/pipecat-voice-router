# Pipecat voice router scaffold

Chosen framework: **Pipecat**.

Reason: for this use case we want custom routing between final ASR text and the main LLM/tools. Pipecat is a Python pipeline framework, so it is a better first fit than LiveKit's heavier realtime/session infrastructure.

Desired flow:

```text
mic/audio transport -> VAD -> STT -> final transcript -> router -> action branch
```

Router branches:

- `scroll up/down` -> direct desktop function, no TTS
- `make full screen` / `fullscreen` -> i3 `fullscreen toggle` for the focused window, no TTS
- `exit fullscreen` -> i3 `fullscreen disable` for the focused window, no TTS
- `list all routed commands` -> lists direct voice commands, TTS yes
- `open youtube and search for X` -> browser automation, no TTS
- `search for X and tell me about it` -> Pig/main LLM, TTS yes
- anything else -> fallback to Pig/main LLM, TTS yes

The first router should mostly be deterministic with room for tiny phrase variations. If the utterance is not a known direct command, do not over-classify it: send it to the main LLM/Pig.

Current STT thought: Moonshine / the small "moon" streaming STT model family is a good candidate for Pi voice, but the STT model is not locked yet.

## Local clone

Pipecat cloned at:

```text
/home/bot/pipecat
```

## Files

- `router_config.json` routing/function config and command list
- `voice_router.py` standalone router/action scaffold

## Current directly routed commands

Defined in `router_config.json`:

- `scroll down`, `go down`, `page down`, `move down`
- `scroll up`, `go up`, `page up`, `move up`
- `make full screen`, `make fullscreen`, `fullscreen`, `full screen`, `toggle full screen`, `toggle fullscreen`
- `exit fullscreen`, `exit full screen`, `leave fullscreen`, `leave full screen`, `disable fullscreen`, `disable full screen`
- `list all routed commands`, `list routed commands`, `what commands can i say`, `show routed commands`, `show voice commands`
- `open youtube and search for ...`
- `search for ... tell me about ...`

## Next setup

Create a Python venv and install Pipecat once we pick exact audio/STT/TTS providers.

Possible install shape:

```bash
cd /home/bot/pipecat
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

#!/usr/bin/env python3
"""Set/read Pipecat voice status for i3bar.

Usage:
  voice_status.py enabled on|off
  voice_status.py hearing on|off
  voice_status.py mode idle|listening|thinking|speaking|error
  voice_status.py profile "pipecat pig-io"|"pipecat paste"
  voice_status.py mic auto-usb|NAME
  voice_status.py show
"""
import json
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cache" / "pipecat-voice"
STATE_FILE = STATE_DIR / "status.json"
DEFAULT = {"enabled": False, "hearing": False, "mode": "off", "profile": "pipecat pig-io", "mic": "auto-usb"}


def load():
    try:
        return {**DEFAULT, **json.loads(STATE_FILE.read_text())}
    except Exception:
        return dict(DEFAULT)


def save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(STATE_FILE)


def main(argv):
    state = load()
    if len(argv) == 1 or argv[1] == "show":
        print(json.dumps(state))
        return
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)
    key, value = argv[1], argv[2].lower()
    if key in {"enabled", "hearing"}:
        state[key] = value in {"1", "true", "yes", "on"}
        if key == "enabled" and not state[key]:
            state["hearing"] = False
            state["mode"] = "off"
        elif key == "enabled" and state[key] and state.get("mode") == "off":
            state["mode"] = "idle"
    elif key == "mode":
        state["mode"] = value
        if value == "off":
            state["enabled"] = False
            state["hearing"] = False
    elif key == "profile":
        if value not in {"pipecat pig-io", "pipecat paste"}:
            raise SystemExit(f"unknown profile: {value}")
        state["profile"] = value
    elif key == "mic":
        state["mic"] = argv[2]
    else:
        raise SystemExit(f"unknown key: {key}")
    save(state)


if __name__ == "__main__":
    main(sys.argv)

#!/usr/bin/env python3
"""Persistent Kokoro synthesis/playback worker using JSON lines on stdin/stdout."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

DOC_TTS_HOME = Path(os.environ.get("DOC_TTS_HOME", "/home/bot/doc-tts"))
sys.path.insert(0, str(DOC_TTS_HOME))

from doc_tts.synth import SynthConfig, Synthesizer, write_wav  # noqa: E402

SYNTH_QUEUE: queue.Queue[dict | None] = queue.Queue()
AUDIO_QUEUE: queue.Queue[dict | None] = queue.Queue()
STATE_LOCK = threading.Lock()
PLAYER_LOCK = threading.Lock()
CANCELLED_THROUGH = 0
CURRENT_PLAYER: subprocess.Popen | None = None
CACHE_DIR = Path(tempfile.gettempdir()) / "pipecat-kokoro"


def emit(event: str, **data) -> None:
    print(json.dumps({"event": event, **data}), flush=True)


def resolve_sink() -> str | None:
    override = os.environ.get("DOC_TTS_SINK")
    if override:
        return override
    try:
        output = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) > 1 and "hdmi" in fields[1]:
            return fields[1]
    return None


def is_cancelled(turn: int) -> bool:
    with STATE_LOCK:
        return turn <= CANCELLED_THROUGH


def cancel_through(turn: int) -> None:
    global CANCELLED_THROUGH
    with STATE_LOCK:
        CANCELLED_THROUGH = max(CANCELLED_THROUGH, turn)
    with PLAYER_LOCK:
        player = CURRENT_PLAYER
    if player and player.poll() is None:
        player.terminate()


def synth_loop() -> None:
    cfg = SynthConfig(
        voice=os.environ.get("VOICE_ROUTER_TTS_VOICE", "af_heart"),
        speed=float(os.environ.get("VOICE_ROUTER_TTS_SPEED", "1.0")),
        device=os.environ.get("VOICE_ROUTER_TTS_DEVICE") or None,
    )
    synth = Synthesizer(cfg)
    synth.speak_text("Ready.")  # Warm the first inference before live speech.
    emit("ready", device=synth.device)
    while True:
        item = SYNTH_QUEUE.get()
        if item is None:
            AUDIO_QUEUE.put(None)
            return
        turn = int(item["turn"])
        if is_cancelled(turn):
            continue
        try:
            audio = synth.speak_text(item["text"])
            if is_cancelled(turn) or not audio.size:
                continue
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = CACHE_DIR / f"turn-{turn}-{item['seq']}.wav"
            write_wav(str(path), audio)
            AUDIO_QUEUE.put({**item, "path": str(path)})
            emit("synthesized", turn=turn, seq=item["seq"], chars=len(item["text"]))
        except Exception as exc:
            emit("error", phase="synthesis", error=str(exc), turn=turn)


def playback_loop() -> None:
    global CURRENT_PLAYER
    sink = resolve_sink()
    while True:
        item = AUDIO_QUEUE.get()
        if item is None:
            return
        path = Path(item["path"])
        turn = int(item["turn"])
        try:
            if is_cancelled(turn):
                continue
            if shutil.which("paplay"):
                command = ["paplay", *(["--device", sink] if sink else []), str(path)]
            else:
                command = ["aplay", "-q", str(path)]
            with PLAYER_LOCK:
                if is_cancelled(turn):
                    continue
                CURRENT_PLAYER = subprocess.Popen(command)
                player = CURRENT_PLAYER
            emit(
                "play_start",
                turn=turn,
                seq=item["seq"],
                chars=len(item["text"]),
                text=item["text"],
            )
            returncode = player.wait()
            emit("play_end", turn=turn, seq=item["seq"], cancelled=returncode != 0 or is_cancelled(turn))
        except Exception as exc:
            emit("error", phase="playback", error=str(exc), turn=turn)
        finally:
            with PLAYER_LOCK:
                CURRENT_PLAYER = None
            path.unlink(missing_ok=True)


def main() -> int:
    threading.Thread(target=synth_loop, name="kokoro-synthesis", daemon=True).start()
    threading.Thread(target=playback_loop, name="kokoro-playback", daemon=True).start()
    for line in sys.stdin:
        try:
            command = json.loads(line)
            op = command.get("op")
            if op == "speak":
                SYNTH_QUEUE.put(command)
            elif op == "cancel":
                cancel_through(int(command.get("through", 0)))
            elif op == "shutdown":
                cancel_through(2**31 - 1)
                SYNTH_QUEUE.put(None)
                return 0
        except Exception as exc:
            emit("error", phase="command", error=str(exc))
    cancel_through(2**31 - 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

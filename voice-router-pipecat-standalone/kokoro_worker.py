#!/usr/bin/env python3
"""Persistent Kokoro synthesis/playback worker using JSON lines on stdin/stdout."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

DOC_TTS_HOME = Path(os.environ.get("DOC_TTS_HOME", "/home/bot/doc-tts"))
sys.path.insert(0, str(DOC_TTS_HOME))

from doc_tts.synth import SAMPLE_RATE, SynthConfig, Synthesizer, write_wav  # noqa: E402

SYNTH_QUEUE: queue.Queue[dict | None] = queue.Queue()
AUDIO_QUEUE: queue.Queue[dict | None] = queue.Queue()
STATE_LOCK = threading.Lock()
PLAYER_LOCK = threading.Lock()
CANCELLED_THROUGH = 0
CURRENT_PLAYER: subprocess.Popen | None = None
CACHE_DIR = Path(tempfile.gettempdir()) / "pipecat-kokoro"
KEEP_LEADING_MS = int(os.environ.get("VOICE_ROUTER_TTS_KEEP_LEADING_MS", "40"))
KEEP_TRAILING_MS = int(os.environ.get("VOICE_ROUTER_TTS_KEEP_TRAILING_MS", "100"))


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


def boundary_silence_ms(audio: np.ndarray, frame_ms: int = 5) -> tuple[int, int]:
    """Measure low-energy leading and trailing frames in a synthesized waveform."""
    frame_samples = max(1, int(SAMPLE_RATE * frame_ms / 1000))
    usable = len(audio) - (len(audio) % frame_samples)
    if usable <= 0:
        return 0, 0
    frames = np.asarray(audio[:usable], dtype=np.float32).reshape(-1, frame_samples)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    peak = float(np.max(np.abs(audio)))
    threshold = max(0.0015, peak * 0.01)
    active = np.flatnonzero(rms >= threshold)
    if not active.size:
        return round(len(audio) * 1000 / SAMPLE_RATE), 0
    leading_ms = int(active[0]) * frame_ms
    trailing_ms = (len(frames) - int(active[-1]) - 1) * frame_ms
    return leading_ms, trailing_ms


def trim_boundary_silence(
    audio: np.ndarray,
    leading_ms: int,
    trailing_ms: int,
) -> tuple[np.ndarray, int]:
    """Trim measured boundary silence while retaining short natural margins."""
    trim_leading_ms = max(0, leading_ms - KEEP_LEADING_MS)
    trim_trailing_ms = max(0, trailing_ms - KEEP_TRAILING_MS)
    start = round(trim_leading_ms * SAMPLE_RATE / 1000)
    end = len(audio) - round(trim_trailing_ms * SAMPLE_RATE / 1000)
    if start >= end:
        return audio, 0
    trimmed = np.asarray(audio[start:end], dtype=np.float32)
    trimmed_ms = round((len(audio) - len(trimmed)) * 1000 / SAMPLE_RATE)
    return trimmed, trimmed_ms


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
        # A stopped process cannot handle SIGTERM until it is continued.
        try:
            player.send_signal(signal.SIGCONT)
        except ProcessLookupError:
            pass


def pause_playback() -> None:
    with PLAYER_LOCK:
        player = CURRENT_PLAYER
    if player and player.poll() is None:
        player.send_signal(signal.SIGSTOP)
        emit("play_paused")


def resume_playback() -> None:
    with PLAYER_LOCK:
        player = CURRENT_PLAYER
    if player and player.poll() is None:
        player.send_signal(signal.SIGCONT)
        emit("play_resumed")


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
            synth_started = time.monotonic()
            audio = synth.speak_text(item["text"])
            synth_ms = round((time.monotonic() - synth_started) * 1000)
            if is_cancelled(turn) or not audio.size:
                continue
            original_audio_ms = round(len(audio) * 1000 / SAMPLE_RATE)
            original_leading_ms, original_trailing_ms = boundary_silence_ms(audio)
            audio, trimmed_ms = trim_boundary_silence(
                audio, original_leading_ms, original_trailing_ms
            )
            audio_ms = round(len(audio) * 1000 / SAMPLE_RATE)
            leading_ms, trailing_ms = boundary_silence_ms(audio)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = CACHE_DIR / f"turn-{turn}-{item['seq']}.wav"
            write_wav(str(path), audio)
            AUDIO_QUEUE.put(
                {
                    **item,
                    "path": str(path),
                    "audio_ms": audio_ms,
                    "original_audio_ms": original_audio_ms,
                    "trimmed_ms": trimmed_ms,
                    "synth_ms": synth_ms,
                    "leading_ms": leading_ms,
                    "trailing_ms": trailing_ms,
                }
            )
            emit(
                "synthesized",
                turn=turn,
                seq=item["seq"],
                chars=len(item["text"]),
                audio_ms=audio_ms,
                original_audio_ms=original_audio_ms,
                trimmed_ms=trimmed_ms,
                synth_ms=synth_ms,
                leading_ms=leading_ms,
                trailing_ms=trailing_ms,
            )
        except Exception as exc:
            emit("error", phase="synthesis", error=str(exc), turn=turn)


def playback_loop() -> None:
    global CURRENT_PLAYER
    sink = resolve_sink()
    previous_play_end: float | None = None
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
            play_started = time.monotonic()
            handoff_ms = (
                round((play_started - previous_play_end) * 1000)
                if previous_play_end is not None
                else None
            )
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
                audio_ms=item.get("audio_ms"),
                original_audio_ms=item.get("original_audio_ms"),
                trimmed_ms=item.get("trimmed_ms"),
                synth_ms=item.get("synth_ms"),
                handoff_ms=handoff_ms,
                leading_ms=item.get("leading_ms"),
                trailing_ms=item.get("trailing_ms"),
            )
            returncode = player.wait()
            previous_play_end = time.monotonic()
            emit(
                "play_end",
                turn=turn,
                seq=item["seq"],
                cancelled=returncode != 0 or is_cancelled(turn),
                playback_ms=round((previous_play_end - play_started) * 1000),
                audio_ms=item.get("audio_ms"),
            )
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
            elif op == "pause":
                pause_playback()
            elif op == "resume":
                resume_playback()
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

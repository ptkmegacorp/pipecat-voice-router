#!/usr/bin/env python3
"""Standalone Pipecat voice router.

Flow:
  local mic -> Silero VAD -> Moonshine STT -> deterministic router -> i3/overlay action or Pig LLM fallback
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import audioop
from difflib import SequenceMatcher
from pathlib import Path

import requests
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.moonshine.stt import MoonshineSTTService, MoonshineSTTSettings, Model
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

ROOT = Path(__file__).resolve().parent
ROUTING_DIR = ROOT.parent / "voice-router-pipecat"
if str(ROUTING_DIR) not in sys.path:
    sys.path.insert(0, str(ROUTING_DIR))
from routing import route_text  # noqa: E402
from audio_input import (  # noqa: E402
    INPUT_CHANNELS,
    INPUT_SAMPLE_RATE,
    discover_input_device_index,
    input_device_name,
    list_input_devices,
)

STATUS = ROUTING_DIR / "voice_status.py"
CONFIG = json.loads((ROUTING_DIR / "router_config.json").read_text())
PID_FILE = ROOT / "voice-router.pid"
LOG_FILE = ROOT / "voice-router.log"

LLM_BASE_URL = os.environ.get("VOICE_ROUTER_LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("VOICE_ROUTER_LLM_MODEL", "")
TTS_CMD = os.environ.get("VOICE_ROUTER_TTS_CMD", "")  # e.g. 'spd-say' or 'espeak'
MOONSHINE_MODEL = os.environ.get("VOICE_ROUTER_MOONSHINE_MODEL", Model.TINY_STREAMING.value)
LLM_MAX_TOKENS = int(os.environ.get("VOICE_ROUTER_LLM_MAX_TOKENS", "64"))
PIG_IO_URL = os.environ.get("VOICE_ROUTER_PIG_IO_URL", "http://127.0.0.1:8765").rstrip("/")
PIG_IO_TIMEOUT = float(os.environ.get("VOICE_ROUTER_PIG_IO_TIMEOUT", "5"))
KOKORO_PYTHON = os.environ.get("VOICE_ROUTER_KOKORO_PYTHON", "/home/bot/doc-tts/.venv/bin/python")
KOKORO_WORKER = Path(os.environ.get("VOICE_ROUTER_KOKORO_WORKER", ROOT / "kokoro_worker.py"))
TTS_STATE_FILE = Path(os.environ.get("VOICE_ROUTER_TTS_STATE", Path.home() / ".cache/pipecat-voice/tts.json"))
TTS_MAX_CHARS = int(os.environ.get("VOICE_ROUTER_TTS_MAX_CHARS", "2400"))
TTS_CHUNK_CHARS = int(os.environ.get("VOICE_ROUTER_TTS_CHUNK_CHARS", "180"))
TTS_PAUSE_SECONDS = float(os.environ.get("VOICE_ROUTER_TTS_PAUSE_SECONDS", "0.65"))
WM_MSG = ["/home/bot/.config/i3/bin/wm-msg.sh"]
SPEAKER = None


def wm(*args: str, check: bool = False) -> None:
    subprocess.run(WM_MSG + list(args), check=check)


def wm_popen(*args: str) -> None:
    subprocess.Popen(WM_MSG + list(args))


def discover_llama_server() -> tuple[str, str]:
    """Use the currently running local llama-server.

    Priority:
    1. explicit VOICE_ROUTER_LLM_BASE_URL / VOICE_ROUTER_LLM_MODEL
    2. scan known local llama.cpp ports and use the first /v1/models response
    """
    explicit_base = os.environ.get("VOICE_ROUTER_LLM_BASE_URL")
    explicit_model = os.environ.get("VOICE_ROUTER_LLM_MODEL")
    candidates = [explicit_base] if explicit_base else []
    candidates += [
        "http://127.0.0.1:8091/v1",  # Pig/local Gemma default on this machine
        "http://127.0.0.1:8091/v1",  # Pig default (Gemma 12B QAT)
        "http://127.0.0.1:8088/v1",  # Qwen text recipe
        "http://127.0.0.1:8080/v1",
    ]
    seen = set()
    for base in [c for c in candidates if c and not (c in seen or seen.add(c))]:
        try:
            r = requests.get(f"{base}/models", timeout=2)
            r.raise_for_status()
            data = r.json()
            models = data.get("data") or data.get("models") or []
            if not models:
                continue
            first = models[0]
            model = explicit_model or first.get("id") or first.get("model") or first.get("name") or "local"
            return base, model
        except Exception:
            continue
    return explicit_base or "http://127.0.0.1:8091/v1", explicit_model or "local"

logger.remove()
logger.add(sys.stderr, level=os.environ.get("VOICE_ROUTER_LOG_LEVEL", "INFO"))
logger.add(str(LOG_FILE), rotation="1 MB", retention=5, level="DEBUG")


def set_status(key: str, value: str):
    subprocess.run([str(STATUS), key, value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def notify(msg: str):
    logger.info(msg)
    try:
        subprocess.run(["notify-send", "Pipecat voice", msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        pass


class SpeechChunker:
    """Turn streamed model deltas into prompt sentence-sized speech chunks."""

    def __init__(self):
        self.buffer = ""
        self.accepted_chars = 0
        self.emitted_chunks = 0

    def add(self, delta: str) -> None:
        remaining = max(0, TTS_MAX_CHARS - self.accepted_chars)
        addition = delta[:remaining]
        self.buffer += addition
        self.accepted_chars += len(addition)

    def take_ready(self, force: bool = False, paused: bool = False) -> list[str]:
        chunks: list[str] = []
        while self.buffer:
            sentences = list(re.finditer(r"(?<!\d)[.!?](?:[\"')\]]*)\s+", self.buffer))
            if sentences:
                end = sentences[0].end()
                if self.emitted_chunks and not force:
                    for boundary in sentences:
                        end = boundary.end()
                        if len(self.clean(self.buffer[:end])) >= 32:
                            break
                    if len(self.clean(self.buffer[:end])) < 32:
                        if len(self.buffer) >= TTS_CHUNK_CHARS:
                            hard_end = self.buffer.rfind(" ", 0, TTS_CHUNK_CHARS + 1)
                            end = hard_end if hard_end > end else TTS_CHUNK_CHARS
                        else:
                            break
            elif len(self.buffer) >= TTS_CHUNK_CHARS:
                end = self.buffer.rfind(" ", 0, TTS_CHUNK_CHARS + 1)
                end = end if end > 0 else TTS_CHUNK_CHARS
            elif force or (paused and len(self.buffer.strip()) >= 40):
                end = len(self.buffer)
            else:
                break
            raw, self.buffer = self.buffer[:end], self.buffer[end:]
            clean = self.clean(raw)
            if clean:
                chunks.append(clean)
                self.emitted_chunks += 1
        return chunks

    @staticmethod
    def clean(text: str) -> str:
        text = re.sub(r"```.*?```", " Code omitted. ", text, flags=re.DOTALL)
        text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
        text = re.sub(r"[`*_#>]", "", text)
        return " ".join(text.split()).strip()


class PigResponseSpeaker:
    """Stream Pig deltas through a persistent, warmed Kokoro worker."""

    def __init__(self):
        self._enabled = self._load_enabled()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._listen, name="pig-tts-events", daemon=True)
        self._worker: subprocess.Popen | None = None
        self._turn = 0
        self._sequence = 0
        self._chunker = SpeechChunker()
        self._pause_timer: threading.Timer | None = None
        self._muted_turn = False
        self._saw_delta = False
        self._current_speech = ""
        self._recent_speech = ""
        self._recent_speech_at = 0.0
        self._awaiting_response = False

    def _load_enabled(self) -> bool:
        try:
            return bool(json.loads(TTS_STATE_FILE.read_text()).get("enabled", False))
        except Exception:
            return False

    def start(self):
        with self._lock:
            self._start_worker()
        self._thread.start()
        logger.info(f"Kokoro streaming TTS starting; enabled={self._enabled} worker={KOKORO_WORKER}")

    def _start_worker(self):
        self._worker = subprocess.Popen(
            [KOKORO_PYTHON, "-u", str(KOKORO_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_worker, name="kokoro-worker-events", daemon=True).start()
        threading.Thread(target=self._read_worker_errors, name="kokoro-worker-errors", daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._cancel_timer()
            self._send_worker({"op": "shutdown"})
            if self._worker and self._worker.poll() is None:
                try:
                    self._worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._worker.terminate()

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def is_likely_echo(self, text: str) -> bool:
        """Recognize a recent Kokoro sentence coming back through the room mic."""
        if not self._speaking.is_set() and time.monotonic() - self._recent_speech_at > 2.0:
            return False
        heard = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
        spoken = " ".join(re.findall(r"[a-z0-9]+", (self._current_speech or self._recent_speech).lower()))
        if not heard or not spoken:
            return False
        return SequenceMatcher(None, heard, spoken).ratio() >= 0.55

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        TTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TTS_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"enabled": enabled}) + "\n")
        tmp.replace(TTS_STATE_FILE)
        if not enabled:
            self.interrupt()

    def interrupt(self):
        """Stop current playback and discard queued speech for this Pig turn."""
        with self._lock:
            logger.info(f"Kokoro TTS interrupting turn={self._turn}")
            self._muted_turn = True
            self._chunker = SpeechChunker()
            self._cancel_timer()
            self._send_worker({"op": "cancel", "through": self._turn})
            self._speaking.clear()

    def speak(self, text: str, force: bool = False):
        if not (force or self._enabled):
            return
        with self._lock:
            self._begin_turn()
            self._chunker.add(text)
            self._queue_chunks(self._chunker.take_ready(force=True))

    def _begin_turn(self):
        self._send_worker({"op": "cancel", "through": self._turn})
        self._turn += 1
        self._sequence = 0
        self._chunker = SpeechChunker()
        self._muted_turn = not self._enabled
        self._saw_delta = False
        self._cancel_timer()

    def _on_user_prompt(self):
        with self._lock:
            self._awaiting_response = True
            self.interrupt()

    def _on_turn_start(self):
        with self._lock:
            if self._awaiting_response:
                self._begin_turn()
                self._awaiting_response = False

    def _on_delta(self, delta: str):
        with self._lock:
            if self._muted_turn or not self._enabled:
                return
            if not self._saw_delta:
                logger.info(f"Kokoro TTS first Pig delta turn={self._turn}")
            self._saw_delta = True
            self._chunker.add(delta)
            self._queue_chunks(self._chunker.take_ready())
            self._schedule_pause_flush()

    def _on_agent_end(self, data: dict):
        logger.info(f"Kokoro TTS Pig agent_end turn={self._turn}")
        with self._lock:
            self._cancel_timer()
            if self._muted_turn or not self._enabled or not data.get("speak", False):
                return
            if not self._saw_delta:
                self._chunker.add(str(data.get("text", "")))
            self._queue_chunks(self._chunker.take_ready(force=True))

    def _queue_chunks(self, chunks: list[str]):
        for text in chunks:
            self._sequence += 1
            logger.info(f"Kokoro TTS queued turn={self._turn} seq={self._sequence} chars={len(text)}")
            self._send_worker(
                {"op": "speak", "turn": self._turn, "seq": self._sequence, "text": text}
            )

    def _schedule_pause_flush(self):
        if not self._chunker.buffer.strip():
            return
        if self._pause_timer and self._pause_timer.is_alive():
            return
        turn = self._turn
        self._pause_timer = threading.Timer(TTS_PAUSE_SECONDS, self._flush_pause, args=(turn,))
        self._pause_timer.daemon = True
        self._pause_timer.start()

    def _flush_pause(self, turn: int):
        with self._lock:
            self._pause_timer = None
            if turn != self._turn or self._muted_turn:
                return
            self._queue_chunks(self._chunker.take_ready(paused=True))
            self._schedule_pause_flush()

    def _cancel_timer(self):
        if self._pause_timer:
            self._pause_timer.cancel()
            self._pause_timer = None

    def _send_worker(self, command: dict):
        try:
            if not self._worker or self._worker.poll() is not None:
                if self._stop.is_set():
                    return
                self._start_worker()
            self._worker.stdin.write(json.dumps(command) + "\n")
            self._worker.stdin.flush()
        except Exception as exc:
            logger.error(f"Kokoro worker command failed: {exc}")

    def _read_worker(self):
        worker = self._worker
        if not worker or not worker.stdout:
            return
        for line in worker.stdout:
            try:
                event = json.loads(line)
                kind = event.get("event")
                if kind == "ready":
                    logger.info(f"Kokoro worker ready on {event.get('device')}")
                elif kind == "play_start":
                    self._current_speech = str(event.get("text", ""))
                    self._speaking.set()
                    set_status("mode", "speaking")
                    logger.info(
                        f"Kokoro playback started turn={event.get('turn')} seq={event.get('seq')} chars={event.get('chars')}"
                    )
                elif kind == "play_end":
                    self._recent_speech = self._current_speech
                    self._recent_speech_at = time.monotonic()
                    self._current_speech = ""
                    self._speaking.clear()
                    set_status("mode", "idle")
                elif kind == "error":
                    logger.error(f"Kokoro worker error: {event}")
                elif kind == "synthesized":
                    logger.debug(f"Kokoro worker synthesized: {event}")
            except Exception as exc:
                logger.warning(f"Invalid Kokoro worker event {line.strip()!r}: {exc}")

    def _read_worker_errors(self):
        worker = self._worker
        if not worker or not worker.stderr:
            return
        for line in worker.stderr:
            logger.debug(f"Kokoro worker: {line.rstrip()}")

    def _listen(self):
        while not self._stop.is_set():
            try:
                with requests.get(f"{PIG_IO_URL}/events", stream=True, timeout=(5, 30)) as response:
                    response.raise_for_status()
                    event_type = ""
                    for line in response.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if not line:
                            event_type = ""
                            continue
                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:]).get("data", {})
                            if event_type == "user_prompt":
                                self._on_user_prompt()
                            elif event_type == "turn_start":
                                self._on_turn_start()
                            elif event_type == "agent_start" and not self._awaiting_response:
                                with self._lock:
                                    self._begin_turn()
                            elif event_type == "text_delta":
                                self._on_delta(str(data.get("delta", "")))
                            elif event_type == "agent_end":
                                self._on_agent_end(data)
            except Exception as exc:
                logger.warning(f"Pig TTS event stream disconnected: {exc}")
                self._stop.wait(2)


def get_focused_window():
    try:
        wid = subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
        name = subprocess.check_output(["xdotool", "getwindowname", wid], text=True).strip()
        klass = ""
        prop = subprocess.check_output(["xprop", "-id", wid, "WM_CLASS"], text=True).strip()
        if "=" in prop:
            vals = prop.split("=", 1)[1].strip()
            parts = [p.strip().strip('"') for p in vals.split(",")]
            klass = parts[-1] if parts else ""
        return {"name": name, "class": klass}
    except Exception:
        return {"name": "", "class": ""}


def scroll_terminal_window(direction: str, repeat: int = 1):
    # Terminal scrollback: Shift+Page_Up/Page_Down.
    key = "Next" if direction == "down" else "Prior"
    if os.environ.get("WAYLAND_DISPLAY"):
        if subprocess.run(["which", "wtype"], capture_output=True).returncode == 0:
            for _ in range(repeat):
                subprocess.run(["wtype", "-M", "shift", "-k", key], check=False)
            return
    xkey = "shift+Next" if direction == "down" else "shift+Prior"
    try:
        wid = subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
        subprocess.run(
            ["xdotool", "key", "--window", wid, "--delay", "40", "--repeat", str(repeat), xkey],
            check=False,
        )
    except Exception:
        pass


def scroll_pig_io_overlay_window(direction: str, repeat: int = 2):
    # The overlay has its own app-level scrollback. Send plain PageUp/PageDown
    # to the app instead of Shift+PageUp terminal scrollback.
    key = "Page_Down" if direction == "down" else "Page_Up"
    try:
        wid = subprocess.check_output(["xdotool", "getactivewindow"], text=True).strip()
        subprocess.run(
            ["xdotool", "key", "--window", wid, "--delay", "40", "--repeat", str(repeat), key],
            check=False,
        )
    except Exception:
        pass


def is_pig_io_overlay(title: str) -> bool:
    return "pig-io-overlay" in (title or "").lower()


def is_pig_hud(title: str) -> bool:
    return "pig-hud" in (title or "").lower()


def is_terminal_like(klass: str, title: str) -> bool:
    klass = (klass or "").lower()
    return "urxvt" in klass or "rxvt" in klass or klass in {"terminal", "xterm"}


def scroll(direction: str):
    win = get_focused_window()
    title = win.get("name") or ""
    klass = win.get("class") or ""
    if is_pig_io_overlay(title) or is_pig_hud(title):
        scroll_pig_io_overlay_window(direction)
        return
    if is_terminal_like(klass, title):
        scroll_terminal_window(direction)
        return

    subprocess.Popen(["xdotool", "key", "Page_Down" if direction == "down" else "Page_Up"])


def close_youtube():
    wm('[app_id="mpv"] kill')
    wm('[class="mpv"] kill')
    subprocess.run(["pkill", "-x", "mpv"], check=False)


def focus_direction(direction: str):
    if direction not in {"left", "right", "up", "down"}:
        raise ValueError(f"invalid focus direction: {direction}")
    wm_popen("focus", direction)


PIG_IO_WORKSPACE = "/home/bot/.config/i3/bin/pig-io-workspace.sh"
PIG_IO_WS = os.environ.get("PIG_IO_WORKSPACE", "1")
MAIN_WS = os.environ.get("PIG_IO_MAIN_WORKSPACE", "2")


def ensure_pig_io_overlay():
    subprocess.Popen(["/home/bot/pig-io/overlay.sh", "show"])


def show_pig_io_workspace():
    ensure_pig_io_overlay()
    if os.path.isfile(PIG_IO_WORKSPACE):
        subprocess.Popen([PIG_IO_WORKSPACE, "show"])
    else:
        wm("workspace", "number", PIG_IO_WS)
        wm('[title="^pig-io-overlay$"]', "focus")


def hide_pig_io_workspace():
    if os.path.isfile(PIG_IO_WORKSPACE):
        subprocess.run([PIG_IO_WORKSPACE, "hide"], check=False)
    else:
        wm("workspace", "number", MAIN_WS)


def focus_pig_io_overlay():
    show_pig_io_workspace()


def open_pig_io_overlay():
    show_pig_io_workspace()


def close_pig_io_overlay():
    hide_pig_io_workspace()
    logger.info(f"switched to workspace {MAIN_WS}")


def open_pig_hud():
    subprocess.Popen(["/home/bot/pig-io/pig-hud.sh", "show"])


def close_pig_hud():
    subprocess.Popen(["/home/bot/pig-io/pig-hud.sh", "close"])


def list_commands() -> str:
    lines = ["direct routed voice commands:"]
    for r in CONFIG["routes"]:
        examples = r.get("match") or [r.get("prefix", "").strip() + "..."]
        lines.append(f"- {', '.join(examples)}")
    lines.append("anything else goes to the main local LLM")
    return "\n".join(lines)


def call_pig_io(prompt: str) -> str | None:
    set_status("mode", "thinking")
    # Show overlay workspace immediately on fallback — don't wait for pig-io /ask round-trip.
    show_pig_io_workspace()
    logger.info(f"Pig fallback using {PIG_IO_URL}")
    try:
        resp = requests.post(
            f"{PIG_IO_URL}/ask",
            json={
                "text": prompt,
                "source": "pipecat_voice",
                "context": {"focused_window": get_focused_window()},
            },
            timeout=PIG_IO_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"pig-io accepted id={data.get('id')} queued={data.get('queued')}")
        # Event streaming/TTS consumption is intentionally handled in a later step.
        return "sent to pig"
    except Exception as e:
        logger.warning(f"pig-io unavailable, falling back to local llama-server: {e}")
        return None


def call_local_llm(prompt: str) -> str:
    set_status("mode", "thinking")
    pig_result = call_pig_io(prompt)
    if pig_result is not None:
        return pig_result
    base_url, model = discover_llama_server()
    logger.info(f"LLM fallback using {base_url} model={model}")
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a concise voice assistant. Answer briefly for speech."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": LLM_MAX_TOKENS,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"local LLM error: {e}"


def speak(text: str, force: bool = False):
    if not text:
        return
    print(text, flush=True)
    if SPEAKER:
        SPEAKER.speak(text, force=force)
    elif TTS_CMD:
        subprocess.Popen([TTS_CMD, text])


def execute(action: dict):
    if action.get("match_method") == "fuzzy":
        logger.info(
            f"fuzzy route score={action.get('match_score')} phrase={action.get('match_phrase')!r} text={action.get('text')!r}"
        )
    logger.info(f"route={action.get('route')} function={action.get('function')} text={action.get('text')!r}")
    fn = action.get("function")
    args = action.get("args", {})
    if fn == "set_tts":
        enabled = bool(args.get("enabled"))
        if not SPEAKER:
            logger.error("Kokoro TTS is not initialized")
            return
        SPEAKER.set_enabled(enabled)
        notify(f"text to speech {'enabled' if enabled else 'disabled'}")
        speak(f"Text to speech is {'on' if enabled else 'off'}.", force=True)
    elif fn == "scroll":
        scroll(args["direction"])
    elif fn == "make_full_screen":
        wm_popen("fullscreen", "toggle")
    elif fn == "exit_full_screen":
        wm_popen("fullscreen", "disable")
    elif fn == "close_youtube":
        close_youtube()
    elif fn == "focus_direction":
        focus_direction(args["direction"])
    elif fn == "open_pig_io_overlay":
        open_pig_io_overlay()
    elif fn == "focus_pig_io_overlay":
        focus_pig_io_overlay()
    elif fn == "close_pig_io_overlay":
        close_pig_io_overlay()
    elif fn == "open_pig_hud":
        open_pig_hud()
    elif fn == "close_pig_hud":
        close_pig_hud()
    elif fn == "list_routed_commands":
        speak(list_commands())
    elif fn in {"ask_pig", "ask_local_llm"}:
        answer = call_local_llm(args.get("prompt", action.get("text", "")))
        if answer != "sent to pig":
            speak(answer)
    else:
        answer = call_local_llm(action.get("text", ""))
        speak(answer)
    if not action.get("tts"):
        set_status("mode", "idle")


class AudioDebugProcessor(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._last_log = 0.0
        self._peak_rms = 0
        self._frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._frames += 1
            try:
                rms = audioop.rms(frame.audio, 2)
                mx = audioop.max(frame.audio, 2)
                self._peak_rms = max(self._peak_rms, rms)
            except Exception:
                rms = mx = 0
            now = time.monotonic()
            if now - self._last_log >= 1.0:
                logger.info(
                    f"audio input frames={self._frames}/s rms={rms} peak_rms={self._peak_rms} max={mx} sr={frame.sample_rate} ch={frame.num_channels} bytes={len(frame.audio)}"
                )
                self._frames = 0
                self._peak_rms = 0
                self._last_log = now
        await self.push_frame(frame, direction)


class ResampleTo16kProcessor(FrameProcessor):
    """Resample local USB mic 48k mono S16 frames to 16k for Silero/Moonshine."""

    def __init__(self):
        super().__init__()
        self._state = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.sample_rate != 16000:
            converted, self._state = audioop.ratecv(
                frame.audio, 2, frame.num_channels, frame.sample_rate, 16000, self._state
            )
            out = InputAudioRawFrame(converted, 16000, frame.num_channels)
            out.pts = frame.pts
            out.transport_source = frame.transport_source
            await self.push_frame(out, direction)
        else:
            await self.push_frame(frame, direction)


class VADStatusProcessor(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._last_vad_stop = 0.0

    async def _idle_if_no_transcript(self, vad_stop_time: float):
        await asyncio.sleep(4.0)
        if self._last_vad_stop == vad_stop_time:
            logger.info("No transcription after VAD stop; returning indicator to idle")
            set_status("mode", "idle")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            # Do not cancel immediately while audio is playing: the room mic can
            # hear Kokoro. A non-echo final transcript performs the barge-in.
            if SPEAKER and not SPEAKER.is_speaking:
                SPEAKER.interrupt()
            set_status("hearing", "on")
            set_status("mode", "listening")
            logger.info("VAD: user started speaking")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            set_status("hearing", "off")
            set_status("mode", "listening")
            logger.info("VAD: user stopped speaking; waiting for Pipecat MoonshineSTTService")
            self._last_vad_stop = time.monotonic()
            asyncio.create_task(self._idle_if_no_transcript(self._last_vad_stop))
        await self.push_frame(frame, direction)


class VoiceRouterProcessor(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            logger.debug(f"interim={frame.text!r}")
        elif isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            logger.info(f"transcription frame={text!r}")
            if text and SPEAKER and SPEAKER.is_likely_echo(text):
                logger.info("ignoring likely Kokoro echo transcription")
                return
            if text:
                if SPEAKER and SPEAKER.is_speaking:
                    SPEAKER.interrupt()
                notify(f"heard: {text}")
                action = route_text(text, {"focused_window": get_focused_window()})
                if action.get("function") in {"ask_pig", "ask_local_llm"}:
                    set_status("mode", "thinking")
                execute(action)
                set_status("mode", "idle")
        else:
            await self.push_frame(frame, direction)


async def main():
    global SPEAKER
    PID_FILE.write_text(str(os.getpid()))
    SPEAKER = PigResponseSpeaker()
    SPEAKER.start()
    set_status("profile", "pipecat pig-io")
    set_status("enabled", "on")
    set_status("mode", "idle")
    base_url, model = discover_llama_server()
    notify(f"standalone Pipecat voice router started; LLM fallback: {base_url} model={model}")

    def shutdown(*_):
        set_status("enabled", "off")
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)

    input_device_index = discover_input_device_index(logger=logger)
    device_label = input_device_name(input_device_index)
    logger.info(f"Using input_device_index={input_device_index} ({device_label})")
    set_status("mic", device_label)
    if input_device_index is None:
        devices = list_input_devices()
        device_lines = ", ".join(
            f"[{d['index']}] {d['name']}{'' if d['opens'] else ' (cannot open)'}"
            for d in devices
        ) or "none"
        msg = f"no working microphone found; devices: {device_lines}"
        logger.error(msg)
        set_status("mode", "error")
        notify(msg)
        raise SystemExit(1)
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_in_channels=INPUT_CHANNELS,
            input_device_index=input_device_index,
        )
    )
    audio_debug = AudioDebugProcessor()
    resampler = ResampleTo16kProcessor()
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=float(os.environ.get("VOICE_ROUTER_VAD_CONFIDENCE", "0.25")),
                start_secs=float(os.environ.get("VOICE_ROUTER_VAD_START_SECS", "0.08")),
                stop_secs=float(os.environ.get("VOICE_ROUTER_VAD_STOP_SECS", "0.9")),
                min_volume=float(os.environ.get("VOICE_ROUTER_VAD_MIN_VOLUME", "0.001")),
            )
        )
    )
    vad_status = VADStatusProcessor()
    stt = MoonshineSTTService(settings=MoonshineSTTSettings(model=MOONSHINE_MODEL))
    logger.info(f"Moonshine STT model={MOONSHINE_MODEL}")
    router = VoiceRouterProcessor()
    pipeline = Pipeline([transport.input(), audio_debug, resampler, vad, vad_status, stt, router])
    # Always-on mic listener: Pipecat defaults to a 5m idle timeout on UserSpeakingFrame
    # and will exit even while audio is flowing. Disable unless explicitly configured.
    idle_timeout = os.environ.get("VOICE_ROUTER_IDLE_TIMEOUT_SECS", "")
    idle_timeout_secs = float(idle_timeout) if idle_timeout.strip() else None
    worker = PipelineWorker(pipeline, idle_timeout_secs=idle_timeout_secs)
    runner = WorkerRunner(handle_sigint=True)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        SPEAKER.stop()
        set_status("enabled", "off")
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    asyncio.run(main())

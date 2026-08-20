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
from collections import deque
from difflib import SequenceMatcher
from pathlib import Path

import requests
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.moonshine.stt import MoonshineSTTService, MoonshineSTTSettings, Model
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

ROOT = Path(__file__).resolve().parent
ROUTING_DIR = ROOT.parent / "voice-router-pipecat"
if str(ROUTING_DIR) not in sys.path:
    sys.path.insert(0, str(ROUTING_DIR))
from pulse_aec import (  # noqa: E402
    AEC_SINK_NAME,
    AEC_SOURCE_NAME,
    cleanup_echo_cancel,
    setup_echo_cancel,
)
from remote_mic import HybridMicInputTransport, RemoteMicHub  # noqa: E402
from routing import route_text  # noqa: E402
from control_http import ControlHooks, start_control, stop_control  # noqa: E402

STATUS = ROUTING_DIR / "voice_status.py"
CONFIG = json.loads((ROUTING_DIR / "router_config.json").read_text())
PID_FILE = ROOT / "voice-router.pid"
LOG_FILE = ROOT / "voice-router.log"

LLM_BASE_URL = os.environ.get("VOICE_ROUTER_LLM_BASE_URL", "")
LLM_MODEL = os.environ.get("VOICE_ROUTER_LLM_MODEL", "")
TTS_CMD = os.environ.get("VOICE_ROUTER_TTS_CMD", "")  # e.g. 'spd-say' or 'espeak'
MOONSHINE_MODEL = os.environ.get("VOICE_ROUTER_MOONSHINE_MODEL", Model.SMALL_STREAMING.value)
LLM_MAX_TOKENS = int(os.environ.get("VOICE_ROUTER_LLM_MAX_TOKENS", "64"))
PIG_IO_URL = os.environ.get("VOICE_ROUTER_PIG_IO_URL", "http://127.0.0.1:8765").rstrip("/")
PIG_IO_TIMEOUT = float(os.environ.get("VOICE_ROUTER_PIG_IO_TIMEOUT", "5"))
FRONTIER_VOICE_URL = os.environ.get("FRONTIER_VOICE_URL", "http://127.0.0.1:8770").rstrip("/")
BACKEND_STATE_FILE = Path(
    os.environ.get(
        "FRONTIER_BACKEND_STATE",
        Path.home() / ".cache/pig-stack-frontier-voice/backend.json",
    )
)
FRONTIER_SH = os.environ.get(
    "FRONTIER_SH",
    "/home/bot/projects/pig-stack-frontier-voice/frontier.sh",
)
THINKING_CADENCE_SECONDS = float(os.environ.get("FRONTIER_THINKING_CADENCE", "4"))
THINKING_REPEAT = os.environ.get("FRONTIER_THINKING_REPEAT", "0") == "1"
BARGE_MIN_VAD_SECONDS = float(os.environ.get("VOICE_ROUTER_BARGE_MIN_VAD_SECS", "0.55"))
BARGE_MIN_RMS = float(os.environ.get("VOICE_ROUTER_BARGE_MIN_RMS", "0.04"))
KOKORO_PYTHON = os.environ.get("VOICE_ROUTER_KOKORO_PYTHON", "/home/bot/doc-tts/.venv/bin/python")
KOKORO_WORKER = Path(os.environ.get("VOICE_ROUTER_KOKORO_WORKER", ROOT / "kokoro_worker.py"))
TTS_STATE_FILE = Path(os.environ.get("VOICE_ROUTER_TTS_STATE", Path.home() / ".cache/pipecat-voice/tts.json"))
TTS_MAX_CHARS = int(os.environ.get("VOICE_ROUTER_TTS_MAX_CHARS", "2400"))
TTS_CHUNK_CHARS = int(os.environ.get("VOICE_ROUTER_TTS_CHUNK_CHARS", "180"))
TTS_PAUSE_SECONDS = float(os.environ.get("VOICE_ROUTER_TTS_PAUSE_SECONDS", "0.65"))
WM_MSG = ["/home/bot/.config/i3/bin/wm-msg.sh"]
SPEAKER = None


def active_voice_backend() -> str:
    """Read ~/.cache/pig-stack-frontier-voice/backend.json; default local."""
    try:
        backend = str(json.loads(BACKEND_STATE_FILE.read_text()).get("backend", "local")).strip().lower()
        if backend in {"local", "frontier"}:
            return backend
    except Exception:
        pass
    return "local"


def active_voice_url() -> str:
    return FRONTIER_VOICE_URL if active_voice_backend() == "frontier" else PIG_IO_URL


def abort_active_voice_backend() -> None:
    url = active_voice_url()
    try:
        requests.post(f"{url}/abort", timeout=2)
        logger.info(f"Posted /abort to {url}")
    except Exception as exc:
        logger.warning(f"Voice /abort failed at {url}: {exc}")


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
        self._turn_speech_chunks = []
        self._recent_turn_speech = ""
        self._recent_speech_at = 0.0
        # Rolling TTS text for echo gating — survives _begin_turn / thinking cadence resets.
        self._echo_memory: list[tuple[float, str]] = []
        self._awaiting_response = False
        self._frontier_turn = False
        self._thinking_stop: threading.Event | None = None
        self._thinking_thread: threading.Thread | None = None
        self._barge_started_at: float | None = None
        self._barge_stopped_at: float | None = None
        self._barge_paused = False
        self._recent_audio_rms: deque[tuple[float, float]] = deque()
        self._pending_frontier_final = ""

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
        self._stop_thinking_cadence()
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

    @property
    def in_frontier_turn(self) -> bool:
        return self._frontier_turn

    # After playback ends, keep guarding briefly — room echo / Moonshine lag.
    TTS_GUARD_TAIL_SECONDS = 4.0
    # While TTS guard is active, only accept barge-in if similarity stays below this.
    CLEAR_NON_ECHO_MAX_SCORE = 0.32
    ECHO_MEMORY_SECONDS = 20.0

    def _normalize_speech(self, value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

    def _remember_spoken(self, text: str) -> None:
        norm = self._normalize_speech(text)
        if not norm:
            return
        now = time.monotonic()
        self._echo_memory.append((now, norm))
        cutoff = now - self.ECHO_MEMORY_SECONDS
        self._echo_memory = [(t, s) for t, s in self._echo_memory if t >= cutoff]

    def _tts_guard_active(self) -> bool:
        """True while Kokoro is playing, or shortly after, or frontier has spoken."""
        if self._speaking.is_set():
            return True
        if self._recent_speech_at and time.monotonic() - self._recent_speech_at <= self.TTS_GUARD_TAIL_SECONDS:
            return True
        return False

    def _echo_candidates(self) -> list[str]:
        chunks = [self._normalize_speech(c) for c in self._turn_speech_chunks if self._normalize_speech(c)]
        candidates = [
            self._normalize_speech(self._current_speech),
            self._normalize_speech(self._recent_speech),
            self._normalize_speech(self._recent_turn_speech),
        ]
        for start in range(len(chunks)):
            for end in range(start + 1, len(chunks) + 1):
                candidates.append(" ".join(chunks[start:end]))
        # Rolling memory (includes thinking... + prior finals wiped by _begin_turn).
        now = time.monotonic()
        cutoff = now - self.ECHO_MEMORY_SECONDS
        mem = [s for t, s in self._echo_memory if t >= cutoff]
        candidates.extend(mem)
        if mem:
            candidates.append(" ".join(mem[-12:]))
        return [c for c in candidates if c]

    def _echo_similarity(self, text: str) -> float:
        heard = self._normalize_speech(text)
        if not heard:
            return 0.0
        candidates = self._echo_candidates()
        if not candidates:
            return 0.0
        return max(SequenceMatcher(None, heard, candidate).ratio() for candidate in candidates)

    def is_likely_echo(self, text: str) -> bool:
        """Recognize recent Kokoro speech returning through the room mic (legacy gate)."""
        if not self._tts_guard_active():
            return False
        score = self._echo_similarity(text)
        if score >= 0.55:
            logger.info(f"Kokoro echo match score={score:.2f} text={text!r}")
            return True
        return False

    def is_clearly_non_echo(self, text: str) -> bool:
        """Barge-in only when transcript is clearly unlike recent TTS.

        Default while TTS guard is active is to ignore STT — Moonshine garble of
        Kokoro often scores mid-range and used to leak through as phantom prompts.
        """
        heard = self._normalize_speech(text)
        if not heard:
            return False
        candidates = self._echo_candidates()
        if not candidates:
            # Playing/frontier but no reference text yet — cannot prove non-echo.
            return False
        score = max(SequenceMatcher(None, heard, candidate).ratio() for candidate in candidates)
        ok = score < self.CLEAR_NON_ECHO_MAX_SCORE
        logger.info(
            f"Kokoro barge-in check score={score:.2f} threshold={self.CLEAR_NON_ECHO_MAX_SCORE} "
            f"accept={ok} text={text!r}"
        )
        return ok

    def should_accept_stt(self, text: str) -> bool:
        """Accept mic transcripts only when idle, or barge-in is clearly non-echo."""
        if not self._tts_guard_active():
            return True
        return self.is_clearly_non_echo(text)

    @property
    def has_barge_candidate(self) -> bool:
        return self._barge_started_at is not None

    def observe_audio_rms(self, rms: int) -> None:
        now = time.monotonic()
        level = rms / 32767.0
        with self._lock:
            self._recent_audio_rms.append((now, level))
            cutoff = now - 0.35
            while self._recent_audio_rms and self._recent_audio_rms[0][0] < cutoff:
                self._recent_audio_rms.popleft()
            # TTS residue often opens VAD before the user speaks, so there may
            # be no second VAD-start event. Upgrade the existing candidate as
            # soon as nearby speech crosses the calibrated energy threshold.
            if (
                self._barge_started_at is not None
                and self._speaking.is_set()
                and not self._barge_paused
                and level >= BARGE_MIN_RMS
            ):
                self._barge_started_at = now
                self._barge_stopped_at = None
                self._barge_paused = True
                self._send_worker({"op": "pause"})
                logger.info(
                    f"Kokoro dynamically paused for barge-in rms={level:.3f} "
                    f"threshold={BARGE_MIN_RMS:.3f}"
                )

    def begin_barge_candidate(self) -> bool:
        """Track TTS-overlap VAD; pause only when mic energy looks like a nearby user."""
        with self._lock:
            if not self._speaking.is_set():
                return False
            peak_rms = max((v for _, v in self._recent_audio_rms), default=0.0)
            if self._barge_started_at is None:
                self._barge_started_at = time.monotonic()
                self._barge_stopped_at = None
            if not self._barge_paused and peak_rms >= BARGE_MIN_RMS:
                # Upgrade an earlier low-energy echo candidate when nearby speech begins.
                self._barge_started_at = time.monotonic()
                self._barge_stopped_at = None
                self._barge_paused = True
                self._send_worker({"op": "pause"})
            logger.info(
                f"Kokoro barge-in candidate peak_rms={peak_rms:.3f} "
                f"threshold={BARGE_MIN_RMS:.3f} paused={self._barge_paused}"
            )
            return True

    def end_barge_candidate(self) -> None:
        with self._lock:
            if self._barge_started_at is not None:
                self._barge_stopped_at = time.monotonic()

    def resolve_barge_candidate(self, text: str) -> bool:
        """Accept speech that continued after ducking, or an explicit stop command."""
        with self._lock:
            if self._barge_started_at is None:
                return self.should_accept_stt(text)
            end = self._barge_stopped_at or time.monotonic()
            duration = end - self._barge_started_at
            heard = self._normalize_speech(text)
            explicit = bool(
                re.match(r"^(please )?(stop|wait|cancel|hold on|never mind)(\b|$)", heard)
            )
            score = self._echo_similarity(text)
            # Once mic energy proves a nearby speaker is present, accept any
            # non-empty phrase. Do not make arbitrary barge-in depend on words
            # or textual similarity to the TTS response.
            accept = explicit or (
                self._barge_paused
                and duration >= BARGE_MIN_VAD_SECONDS
                and bool(heard)
            )
            logger.info(
                f"Kokoro barge-in resolve duration={duration:.2f}s echo_score={score:.2f} "
                f"paused={self._barge_paused} explicit={explicit} accept={accept} text={text!r}"
            )
            pending_final = self._pending_frontier_final
            was_paused = self._barge_paused
            self._pending_frontier_final = ""
            self._barge_started_at = None
            self._barge_stopped_at = None
            self._barge_paused = False
            if not accept:
                if pending_final:
                    self.speak(pending_final)
                elif was_paused:
                    self._send_worker({"op": "resume"})
            return accept

    def resume_barge_candidate(self) -> None:
        """Resume a paused response when VAD produced no usable transcription."""
        with self._lock:
            if self._barge_started_at is None:
                return
            logger.info("Kokoro resuming playback; barge-in produced no transcript")
            pending_final = self._pending_frontier_final
            was_paused = self._barge_paused
            self._pending_frontier_final = ""
            self._barge_started_at = None
            self._barge_stopped_at = None
            self._barge_paused = False
            if pending_final:
                self.speak(pending_final)
            elif was_paused:
                self._send_worker({"op": "resume"})

    def snapshot(self) -> dict:
        return {
            "enabled": bool(self._enabled),
            "speaking": self.is_speaking,
            "turn": self._turn,
            "muted_turn": bool(self._muted_turn),
            "frontier_turn": self.in_frontier_turn,
            "state_file": str(TTS_STATE_FILE),
        }

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
        self._stop_thinking_cadence()
        with self._lock:
            logger.info(f"Kokoro TTS interrupting turn={self._turn}")
            self._frontier_turn = False
            self._barge_started_at = None
            self._barge_stopped_at = None
            self._barge_paused = False
            self._pending_frontier_final = ""
            self._muted_turn = True
            self._chunker = SpeechChunker()
            self._cancel_timer()
            self._send_worker({"op": "cancel", "through": self._turn})
            self._speaking.clear()

    def _stop_thinking_cadence(self):
        if self._thinking_stop is not None:
            self._thinking_stop.set()
        self._thinking_stop = None
        self._thinking_thread = None

    def _start_thinking_cadence(self):
        """Speak one thinking cue; optionally repeat it until agent_end."""
        self._stop_thinking_cadence()
        stop = threading.Event()
        self._thinking_stop = stop

        with self._lock:
            if not self._frontier_turn or self._muted_turn:
                return
            self.speak("thinking...", force=True)
        if not THINKING_REPEAT:
            return

        def loop():
            while not self._stop.is_set() and not stop.wait(THINKING_CADENCE_SECONDS):
                with self._lock:
                    if stop.is_set() or not self._frontier_turn or self._muted_turn:
                        return
                    self.speak("thinking...", force=True)

        thread = threading.Thread(target=loop, name="frontier-thinking-tts", daemon=True)
        self._thinking_thread = thread
        thread.start()

    def speak(self, text: str, force: bool = False):
        if not (force or self._enabled):
            return
        with self._lock:
            self._begin_turn()
            self._chunker.add(text)
            self._queue_chunks(self._chunker.take_ready(force=True))

    def _begin_turn(self):
        self._send_worker({"op": "cancel", "through": self._turn})
        if self._turn_speech_chunks:
            self._recent_turn_speech = " ".join(self._turn_speech_chunks)
            self._recent_speech_at = time.monotonic()
        self._turn_speech_chunks = []
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

    def _on_frontier_agent_start(self):
        # Plain replies: no thinking TTS. Cadence starts only on tool_execution_start.
        logger.info("Kokoro TTS frontier agent_start; waiting for tools or final text")
        with self._lock:
            self._frontier_turn = True
            self._awaiting_response = False
            self._muted_turn = False

    def _on_frontier_tool_start(self, data: dict):
        logger.info(
            f"Kokoro TTS frontier tool_execution_start tool={data.get('toolName')} "
            f"id={data.get('toolCallId')}; starting thinking cadence"
        )
        with self._lock:
            self._frontier_turn = True
            self._awaiting_response = False
            self._muted_turn = False
        self._start_thinking_cadence()

    def _on_frontier_agent_end(self, data: dict):
        logger.info(f"Kokoro TTS frontier agent_end turn={self._turn}")
        self._stop_thinking_cadence()
        with self._lock:
            self._frontier_turn = False
            self._awaiting_response = False
            if self._muted_turn or not self._enabled or not bool(data.get("speak", True)):
                return
            text = str(data.get("text", "")).strip()
            if not text:
                return
            if self._barge_started_at is not None:
                logger.info("Deferring frontier final TTS until barge-in candidate resolves")
                self._pending_frontier_final = text
                return
            # interrupt thinking playback (if any), then speak final text once (RLock-safe)
            self.speak(text)

    def _queue_chunks(self, chunks: list[str]):
        for text in chunks:
            self._sequence += 1
            self._remember_spoken(text)
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
                    if self._current_speech:
                        self._turn_speech_chunks.append(self._current_speech)
                    self._speaking.set()
                    set_status("mode", "speaking")
                    logger.info(
                        f"Kokoro playback started turn={event.get('turn')} seq={event.get('seq')} "
                        f"chars={event.get('chars')} audio_ms={event.get('audio_ms')} "
                        f"trimmed_ms={event.get('trimmed_ms')} synth_ms={event.get('synth_ms')} "
                        f"handoff_ms={event.get('handoff_ms')} "
                        f"leading_ms={event.get('leading_ms')} trailing_ms={event.get('trailing_ms')}"
                    )
                elif kind == "play_end":
                    logger.info(
                        f"Kokoro playback ended turn={event.get('turn')} seq={event.get('seq')} "
                        f"audio_ms={event.get('audio_ms')} playback_ms={event.get('playback_ms')} "
                        f"cancelled={event.get('cancelled')}"
                    )
                    self._recent_speech = self._current_speech
                    self._recent_turn_speech = " ".join(self._turn_speech_chunks)
                    self._recent_speech_at = time.monotonic()
                    self._current_speech = ""
                    self._speaking.clear()
                    set_status("mode", "idle")
                elif kind == "error":
                    logger.error(f"Kokoro worker error: {event}")
                elif kind == "synthesized":
                    logger.info(
                        f"Kokoro synthesized turn={event.get('turn')} seq={event.get('seq')} "
                        f"chars={event.get('chars')} audio_ms={event.get('audio_ms')} "
                        f"original_audio_ms={event.get('original_audio_ms')} "
                        f"trimmed_ms={event.get('trimmed_ms')} synth_ms={event.get('synth_ms')} "
                        f"leading_ms={event.get('leading_ms')} "
                        f"trailing_ms={event.get('trailing_ms')}"
                    )
            except Exception as exc:
                logger.warning(f"Invalid Kokoro worker event {line.strip()!r}: {exc}")

    def _read_worker_errors(self):
        worker = self._worker
        if not worker or not worker.stderr:
            return
        for line in worker.stderr:
            logger.debug(f"Kokoro worker: {line.rstrip()}")

    def _listen(self):
        # Re-read backend each reconnect so enable/disable flips without pipecat restart.
        while not self._stop.is_set():
            backend = active_voice_backend()
            url = FRONTIER_VOICE_URL if backend == "frontier" else PIG_IO_URL
            try:
                logger.info(f"Kokoro TTS connecting to {url}/events backend={backend}")
                with requests.get(f"{url}/events", stream=True, timeout=(5, 30)) as response:
                    response.raise_for_status()
                    event_type = ""
                    for line in response.iter_lines(decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if active_voice_backend() != backend:
                            logger.info("Voice backend flipped; reconnecting SSE")
                            break
                        if not line:
                            event_type = ""
                            continue
                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:]).get("data", {})
                            frontier = backend == "frontier"
                            if event_type == "user_prompt":
                                self._on_user_prompt()
                            elif event_type == "turn_start":
                                if not frontier:
                                    self._on_turn_start()
                            elif event_type == "agent_start":
                                if frontier:
                                    self._on_frontier_agent_start()
                                elif not self._awaiting_response:
                                    with self._lock:
                                        self._begin_turn()
                            elif event_type == "tool_execution_start":
                                if frontier:
                                    self._on_frontier_tool_start(data)
                            elif event_type == "text_delta":
                                if not frontier:
                                    self._on_delta(str(data.get("delta", "")))
                            elif event_type == "agent_end":
                                if frontier or self._frontier_turn:
                                    self._on_frontier_agent_end(data)
                                else:
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
    lines.append("anything else goes to Pig")
    return "\n".join(lines)


def call_pig_io(prompt: str) -> str | None:
    set_status("mode", "thinking")
    backend = active_voice_backend()
    url = FRONTIER_VOICE_URL if backend == "frontier" else PIG_IO_URL
    if backend != "frontier":
        show_pig_io_workspace()
    logger.info(f"Voice ask using {url} backend={backend}")
    try:
        resp = requests.post(
            f"{url}/ask",
            json={
                "text": prompt,
                "source": "pipecat_voice",
                "context": {"focused_window": get_focused_window()},
            },
            timeout=PIG_IO_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"{backend} accepted id={data.get('id')} queued={data.get('queued')}")
        return "sent to pig"
    except Exception as e:
        logger.warning(f"{backend} unavailable at {url}, falling back to local llama-server: {e}")
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
    elif fn == "set_frontier_mode":
        backend = str(args.get("backend", "local")).strip().lower()
        cmd = "enable" if backend == "frontier" else "disable"
        try:
            result = subprocess.run(
                [FRONTIER_SH, cmd],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
                logger.error(f"frontier.sh {cmd} failed: {err}")
                notify(f"frontier {cmd} failed")
                speak(f"Frontier {cmd} failed.", force=True)
                return
            notify(f"frontier {cmd}")
            speak(f"Frontier mode is {'on' if cmd == 'enable' else 'off'}.", force=True)
        except FileNotFoundError:
            logger.error(f"frontier launcher missing: {FRONTIER_SH}")
            speak("Frontier launcher is not installed yet.", force=True)
        except Exception as exc:
            logger.error(f"frontier.sh {cmd} error: {exc}")
            speak(f"Frontier {cmd} failed.", force=True)
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
            if SPEAKER:
                SPEAKER.observe_audio_rms(rms)
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
        if isinstance(frame, StartFrame):
            # Downstream VAD, STT, and Smart Turn all consume the resampled stream.
            frame.audio_in_sample_rate = 16000
            await self.push_frame(frame, direction)
        elif isinstance(frame, InputAudioRawFrame) and frame.sample_rate != 16000:
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
            if SPEAKER:
                SPEAKER.resume_barge_candidate()
            logger.info("No transcription after VAD stop; returning indicator to idle")
            set_status("mode", "idle")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            # Invalidate an older no-transcript timer while a new utterance is active.
            self._last_vad_stop = 0.0
            # Duck active TTS only when recent mic energy is substantially above
            # the measured AEC residue. Final STT still validates the interruption.
            if SPEAKER:
                SPEAKER.begin_barge_candidate()
            set_status("hearing", "on")
            set_status("mode", "listening")
            logger.info("VAD: user started speaking")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if SPEAKER:
                SPEAKER.end_barge_candidate()
            set_status("hearing", "off")
            set_status("mode", "listening")
            logger.info("VAD: user stopped speaking; waiting for Pipecat MoonshineSTTService")
            self._last_vad_stop = time.monotonic()
            asyncio.create_task(self._idle_if_no_transcript(self._last_vad_stop))
        await self.push_frame(frame, direction)


class VoiceRouterProcessor(FrameProcessor):
    """Route only semantically completed Pipecat user turns."""

    def __init__(self):
        super().__init__()
        self._pending_text = ""

    def _route_pending(self) -> None:
        text = self._pending_text
        self._pending_text = ""
        if not text:
            logger.info("Pipecat user turn stopped without an accepted transcript")
            return
        notify(f"heard: {text}")
        action = route_text(text, {"focused_window": get_focused_window()})
        if action.get("function") in {"ask_pig", "ask_local_llm"}:
            set_status("mode", "thinking")
        execute(action)
        set_status("mode", "idle")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._pending_text = ""
            logger.info("Pipecat user turn started")
        elif isinstance(frame, InterimTranscriptionFrame):
            logger.debug(f"interim={frame.text!r}")
            await self.push_frame(frame, direction)
        elif isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            logger.info(f"transcription frame={text!r}")
            if not text:
                await self.push_frame(frame, direction)
                return
            if SPEAKER and SPEAKER.has_barge_candidate:
                if not SPEAKER.resolve_barge_candidate(text):
                    logger.info("ignoring rejected TTS barge-in candidate")
                    await self.push_frame(frame, direction)
                    return
                # The standard turn lifecycle owns interruption propagation;
                # these calls bridge it to external Kokoro and HTTP backends.
                await self.broadcast_interruption()
                SPEAKER.interrupt()
                abort_active_voice_backend()
            elif SPEAKER and not SPEAKER.should_accept_stt(text):
                logger.info("ignoring STT during post-TTS echo guard")
                await self.push_frame(frame, direction)
                return
            self._pending_text = " ".join(part for part in (self._pending_text, text) if part)
            await self.push_frame(frame, direction)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            logger.info("Pipecat user turn stopped")
            self._route_pending()
        else:
            await self.push_frame(frame, direction)


async def main():
    global SPEAKER
    PID_FILE.write_text(str(os.getpid()))
    aec = setup_echo_cancel()
    os.environ["DOC_TTS_SINK"] = AEC_SINK_NAME
    SPEAKER = PigResponseSpeaker()
    SPEAKER.start()
    start_control(
        ControlHooks(
            get_speaker=lambda: SPEAKER,
            execute=execute,
            route_text=lambda text: route_text(text, {"focused_window": get_focused_window()}),
            abort=abort_active_voice_backend,
            get_backend=active_voice_backend,
            routes=list(CONFIG.get("routes") or []),
        )
    )
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

    device_label = f"WebRTC AEC: {aec['source_master']}"
    set_status("mic", device_label)
    mic_hub = RemoteMicHub(
        on_active=lambda on: set_status("mic", "saturn-mic A15" if on else device_label)
    )
    hub_task = asyncio.create_task(mic_hub.serve())
    audio_input = HybridMicInputTransport(AEC_SOURCE_NAME, mic_hub)
    audio_debug = AudioDebugProcessor()
    resampler = ResampleTo16kProcessor()
    vad_params = VADParams(
        confidence=float(os.environ.get("VOICE_ROUTER_VAD_CONFIDENCE", "0.25")),
        start_secs=float(os.environ.get("VOICE_ROUTER_VAD_START_SECS", "0.08")),
        stop_secs=float(os.environ.get("VOICE_ROUTER_VAD_STOP_SECS", "0.9")),
        min_volume=float(os.environ.get("VOICE_ROUTER_VAD_MIN_VOLUME", "0.006")),
    )
    logger.info(
        f"Silero VAD confidence={vad_params.confidence} start_secs={vad_params.start_secs} "
        f"stop_secs={vad_params.stop_secs} min_volume={vad_params.min_volume}"
    )
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=vad_params))
    vad_status = VADStatusProcessor()
    stt = MoonshineSTTService(settings=MoonshineSTTSettings(model=MOONSHINE_MODEL))
    logger.info(f"Moonshine STT model={MOONSHINE_MODEL}")
    turns = UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            # Raw VAD begins audio collection for Smart Turn, but does not
            # interrupt external TTS until the transcript passes barge validation.
            start=[VADUserTurnStartStrategy(enable_interruptions=False)],
        )
    )
    router = VoiceRouterProcessor()
    # Router observes transcription before UserTurnProcessor. Pipecat system
    # frames have priority, so placing it after turns could deliver turn-stop
    # before the transcription data frame it finalizes.
    pipeline = Pipeline([audio_input, audio_debug, resampler, vad, vad_status, stt, router, turns])
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
        try:
            await mic_hub.close()
            await hub_task
        except Exception:
            pass
        SPEAKER.stop()
        stop_control()
        cleanup_echo_cancel()
        set_status("enabled", "off")
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    asyncio.run(main())

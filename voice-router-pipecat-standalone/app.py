#!/usr/bin/env python3
"""Standalone Pipecat voice router.

Flow:
  local mic -> Silero VAD -> Moonshine STT -> deterministic router -> action or local LLM
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import audioop
import urllib.parse
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
WM_MSG = ["/home/bot/.config/i3/bin/wm-msg.sh"]


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
        "http://127.0.0.1:8090/v1",  # Pi audio baseline
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


def scroll_terminal_window(direction: str, repeat: int = 4):
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


def is_terminal_like(klass: str, title: str) -> bool:
    klass = (klass or "").lower()
    title = (title or "").lower()
    return (
        "urxvt" in klass
        or "rxvt" in klass
        or klass in {"terminal", "xterm"}
        or "pig-io-overlay" in title
    )


def scroll(direction: str):
    win = get_focused_window()
    title = win.get("name") or ""
    klass = win.get("class") or ""
    if is_terminal_like(klass, title):
        scroll_terminal_window(direction)
        return

    subprocess.Popen(["xdotool", "key", "Page_Down" if direction == "down" else "Page_Up"])


def open_youtube(query: str):
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    wm_popen("exec", f"firefox --new-window {url}")


def open_firefox():
    wm_popen("exec", "firefox --new-window about:blank")


def close_firefox():
    wm("[app_id=\"firefox\"] kill")
    wm('[class="firefox"] kill')
    wm('[instance="firefox"] kill')


def close_youtube():
    wm('[app_id="mpv"] kill')
    wm('[class="mpv"] kill')
    subprocess.run(["pkill", "-x", "mpv"], check=False)


def focus_direction(direction: str):
    if direction not in {"left", "right", "up", "down"}:
        raise ValueError(f"invalid focus direction: {direction}")
    wm_popen("focus", direction)


def focus_pig_io_overlay():
    subprocess.run(["/home/bot/pig-io/overlay.sh", "show"], check=False)
    wm('[title="^pig-io-overlay$"]', "move to workspace current, sticky enable, focus")


def open_pig_io_overlay():
    subprocess.Popen(["/home/bot/pig-io/overlay.sh", "show"])


def close_pig_io_overlay():
    result = subprocess.run(
        ["/home/bot/pig-io/overlay.sh", "hide"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(f"overlay hide failed code={result.returncode} stderr={result.stderr.strip()!r}")
    else:
        logger.info("overlay hidden")


def list_commands() -> str:
    lines = ["direct routed voice commands:"]
    for r in CONFIG["routes"]:
        examples = r.get("match") or [r.get("prefix", "").strip() + "..."]
        lines.append(f"- {', '.join(examples)}")
    lines.append("anything else goes to the main local LLM")
    return "\n".join(lines)


def call_pig_io(prompt: str) -> str | None:
    set_status("mode", "thinking")
    # Show overlay immediately on fallback — don't wait for pig-io /ask round-trip.
    subprocess.Popen(["/home/bot/pig-io/overlay.sh", "show"])
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


def speak(text: str):
    if not text:
        return
    print(text, flush=True)
    if TTS_CMD:
        set_status("mode", "speaking")
        subprocess.run([TTS_CMD, text], check=False)


def execute(action: dict):
    if action.get("match_method") == "fuzzy":
        logger.info(
            f"fuzzy route score={action.get('match_score')} phrase={action.get('match_phrase')!r} text={action.get('text')!r}"
        )
    logger.info(f"route={action.get('route')} function={action.get('function')} text={action.get('text')!r}")
    fn = action.get("function")
    args = action.get("args", {})
    if fn == "scroll":
        scroll(args["direction"])
    elif fn == "make_full_screen":
        wm_popen("fullscreen", "toggle")
    elif fn == "exit_full_screen":
        wm_popen("fullscreen", "disable")
    elif fn == "open_youtube_search_url":
        open_youtube(args["query"])
    elif fn == "open_firefox":
        open_firefox()
    elif fn == "close_firefox":
        close_firefox()
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
    elif fn == "list_routed_commands":
        speak(list_commands())
    elif fn in {"ask_pig", "ask_local_llm"}:
        answer = call_local_llm(args.get("prompt", action.get("text", "")))
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
            if text:
                notify(f"heard: {text}")
                action = route_text(text, {"focused_window": get_focused_window()})
                if action.get("function") in {"ask_pig", "ask_local_llm"}:
                    set_status("mode", "thinking")
                execute(action)
                set_status("mode", "idle")
        else:
            await self.push_frame(frame, direction)


async def main():
    PID_FILE.write_text(str(os.getpid()))
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
        set_status("enabled", "off")
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    asyncio.run(main())

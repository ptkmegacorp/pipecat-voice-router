#!/usr/bin/env python3
"""Run voice-router actions for Phone Saturn Mic; return text for on-device TTS."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import voice_router as vr

PIG_STACK = os.environ.get("PIG_STACK_BIN", "/home/bot/bin/pig-stack")
FRONTIER_SH = os.environ.get(
    "VOICE_ROUTER_FRONTIER_SH",
    "/home/bot/bin/saturn-frontier-browser-control",
)
STATE_DIR = Path.home() / ".cache/pipecat-voice"
PHONE_TTS_FLAG = STATE_DIR / "phone_tts_enabled"
MAX_SPEAK_CHARS = int(os.environ.get("VOICE_ROUTER_PHONE_MAX_SPEAK_CHARS", "2400"))


def phone_tts_enabled() -> bool:
    if not PHONE_TTS_FLAG.exists():
        return True
    return PHONE_TTS_FLAG.read_text().strip() != "0"


def set_phone_tts(enabled: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PHONE_TTS_FLAG.write_text("1" if enabled else "0")


def execute_routed(action: dict) -> list[str]:
    """Perform the routed action on Saturn; return lines for the phone speaker."""
    speak: list[str] = []
    fn = action.get("function")
    args = action.get("args") or {}
    want_tts = bool(action.get("tts"))

    if fn == "set_tts":
        enabled = bool(args.get("enabled"))
        set_phone_tts(enabled)
        if want_tts:
            speak.append(f"Text to speech is {'on' if enabled else 'off'}.")
        return speak

    if fn == "set_frontier_mode":
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
                speak.append(f"Frontier {cmd} failed.")
                raise RuntimeError(err)
            if want_tts:
                speak.append(f"Frontier mode is {'on' if cmd == 'enable' else 'off'}.")
        except FileNotFoundError:
            speak.append("Frontier launcher is not installed yet.")
        except subprocess.TimeoutExpired:
            speak.append(f"Frontier {cmd} timed out.")
        return speak

    if fn == "scroll":
        vr.scroll(args["direction"], action.get("context"))
        return speak
    if fn == "make_full_screen":
        vr.make_full_screen()
        return speak
    if fn == "exit_full_screen":
        vr.exit_full_screen()
        return speak
    if fn == "focus_direction":
        vr.focus_direction(args["direction"])
        return speak
    if fn == "close_youtube":
        vr.close_youtube()
        return speak
    if fn == "open_pig_io_overlay":
        vr.open_pig_io_overlay()
        return speak
    if fn == "focus_pig_io_overlay":
        vr.focus_pig_io_overlay()
        return speak
    if fn == "close_pig_io_overlay":
        vr.close_pig_io_overlay()
        return speak
    if fn == "open_pig_hud":
        vr.open_pig_hud()
        return speak
    if fn == "close_pig_hud":
        vr.close_pig_hud()
        return speak
    if fn == "list_routed_commands":
        text = vr.list_routed_commands()
        if want_tts and text:
            speak.append(text)
        return speak

    if fn in {"ask_pig", "ask_local_llm"} or not fn:
        prompt = str(args.get("prompt") or action.get("text") or "").strip()
        if not prompt:
            return speak
        if not phone_tts_enabled():
            subprocess.run(
                [PIG_STACK, "ask", prompt],
                check=False,
                timeout=30,
            )
            return speak
        try:
            out = subprocess.run(
                [PIG_STACK, "ask", "--wait", "--timeout", "120", prompt],
                capture_output=True,
                text=True,
                timeout=130,
                check=False,
            )
            text = (out.stdout or "").strip()
            if text:
                speak.append(text[:MAX_SPEAK_CHARS])
            elif out.returncode != 0:
                speak.append("Pig did not respond.")
        except subprocess.TimeoutExpired:
            speak.append("Pig timed out.")
        return speak

    raise ValueError(f"unknown voice route function: {fn}")

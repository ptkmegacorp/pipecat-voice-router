#!/usr/bin/env python3
"""Robust microphone discovery for Pipecat voice router.

PyAudio device indices change across reboots. Prefer stable name/ALSA matching
over numeric indices. Optional config: ~/.config/pipecat-voice/input.json
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

INPUT_SAMPLE_RATE = 48000
INPUT_CHANNELS = 1

CONFIG_FILE = Path.home() / ".config" / "pipecat-voice" / "input.json"
CACHE_FILE = Path.home() / ".cache" / "pipecat-voice" / "last-input-device.json"

DEFAULT_MATCH = ("USB Composite Device", "USB Audio")
DEFAULT_EXCLUDE = ("hdmi", "nvidia", "monitor", "50r6", "spdif", "iec958")

_HW_RE = re.compile(r"\(hw:(\d+),\d+\)")
_CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[[^\]]+\]:\s+(\S+)\s+-\s+(.+)$")


def _split_patterns(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def match_patterns() -> tuple[str, ...]:
    cfg = _load_config()
    env = os.environ.get("VOICE_ROUTER_INPUT_DEVICE_MATCH", "").strip()
    if env:
        return _split_patterns(env)
    cfg_match = cfg.get("match_patterns")
    if cfg_match:
        return tuple(str(p) for p in cfg_match if str(p).strip())
    return DEFAULT_MATCH


def exclude_patterns() -> tuple[str, ...]:
    cfg = _load_config()
    env = os.environ.get("VOICE_ROUTER_INPUT_DEVICE_EXCLUDE", "").strip()
    if env:
        return tuple(p.lower() for p in _split_patterns(env))
    cfg_exclude = cfg.get("exclude_patterns")
    if cfg_exclude:
        return tuple(str(p).lower() for p in cfg_exclude if str(p).strip())
    return DEFAULT_EXCLUDE


def last_working_name() -> str:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return str(data.get("name", "")).strip()
    except Exception:
        return ""


def save_last_working_device(name: str, index: int) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"name": name, "index": index}, sort_keys=True))
    tmp.replace(CACHE_FILE)


def alsa_usb_capture_cards() -> dict[int, str]:
    """Map ALSA card number -> long name for USB capture cards."""
    cards: dict[int, str] = {}
    try:
        for line in Path("/proc/asound/cards").read_text().splitlines():
            m = _CARD_LINE_RE.match(line)
            if not m:
                continue
            card_no = int(m.group(1))
            driver = m.group(2).lower()
            long_name = m.group(3).strip()
            if driver == "usb-audio" or "usb" in long_name.lower():
                cards[card_no] = long_name
    except Exception:
        pass
    return cards


def _hw_card(name: str) -> int | None:
    m = _HW_RE.search(name)
    return int(m.group(1)) if m else None


def _probe_input_device(p, index: int) -> bool:
    import pyaudio

    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=INPUT_CHANNELS,
            rate=INPUT_SAMPLE_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=960,
        )
        stream.close()
        return True
    except Exception:
        return False


def _score_device(name: str, patterns: tuple[str, ...], usb_cards: dict[int, str]) -> int:
    lower = name.lower()
    score = 0
    hw = _hw_card(name)

    for rank, pattern in enumerate(patterns):
        if pattern.lower() in lower:
            score += 200 - rank * 10

    last = last_working_name()
    if last and last.lower() in lower:
        score += 80
    elif last and lower in last.lower():
        score += 60

    if hw is not None and hw in usb_cards:
        score += 70
        card_name = usb_cards[hw].lower()
        for pattern in patterns:
            if pattern.lower() in card_name:
                score += 40

    if "usb" in lower:
        score += 30

    return score


def list_input_devices() -> list[dict]:
    import pyaudio

    p = pyaudio.PyAudio()
    try:
        devices = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] >= INPUT_CHANNELS:
                devices.append(
                    {
                        "index": i,
                        "name": info["name"],
                        "channels": int(info["maxInputChannels"]),
                        "sample_rate": float(info["defaultSampleRate"]),
                        "opens": _probe_input_device(p, i),
                    }
                )
        return devices
    finally:
        p.terminate()


def discover_input_device_index(logger=None) -> int | None:
    """Pick a working mic using name/ALSA matching; index env is a fallback hint only."""
    import pyaudio

    patterns = match_patterns()
    excludes = exclude_patterns()
    usb_cards = alsa_usb_capture_cards()

    p = pyaudio.PyAudio()
    try:
        explicit = os.environ.get("VOICE_ROUTER_INPUT_DEVICE_INDEX", "").strip()
        if explicit:
            idx = int(explicit)
            if _probe_input_device(p, idx):
                name = p.get_device_info_by_index(idx)["name"]
                if logger:
                    logger.info(f"using configured input_device_index={idx} ({name})")
                save_last_working_device(name, idx)
                return idx
            if logger:
                logger.warning(
                    f"configured input_device_index={idx} unavailable; auto-detecting by name"
                )

        ranked: list[tuple[int, str, int]] = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] < INPUT_CHANNELS:
                continue
            name = info["name"]
            lower = name.lower()
            if any(ex in lower for ex in excludes):
                continue
            score = _score_device(name, patterns, usb_cards)
            if score > 0:
                ranked.append((score, i, name))

        ranked.sort(key=lambda row: (-row[0], row[1]))

        for score, i, name in ranked:
            if _probe_input_device(p, i):
                if logger:
                    logger.info(f"auto-selected mic index={i} score={score} name={name!r}")
                save_last_working_device(name, i)
                return i

        default_idx = int(p.get_default_input_device_info()["index"])
        if _probe_input_device(p, default_idx):
            name = p.get_device_info_by_index(default_idx)["name"]
            if logger:
                logger.info(f"falling back to system default mic index={default_idx} ({name})")
            save_last_working_device(name, default_idx)
            return default_idx

        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["maxInputChannels"] >= INPUT_CHANNELS and _probe_input_device(p, i):
                name = info["name"]
                if logger:
                    logger.warning(f"last-resort mic index={i} ({name})")
                save_last_working_device(name, i)
                return i
    finally:
        p.terminate()
    return None


def input_device_name(index: int | None) -> str:
    if index is None:
        return "system default"
    import pyaudio

    p = pyaudio.PyAudio()
    try:
        return p.get_device_info_by_index(index)["name"]
    except Exception:
        return f"device {index}"
    finally:
        p.terminate()


if __name__ == "__main__":
    import sys

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        for d in list_input_devices():
            tag = "ok" if d["opens"] else "fail"
            print(f"[{d['index']:>2}] {tag:4} {d['name']}")
    else:
        idx = discover_input_device_index(logger=logger)
        print(input_device_name(idx) if idx is not None else "none")

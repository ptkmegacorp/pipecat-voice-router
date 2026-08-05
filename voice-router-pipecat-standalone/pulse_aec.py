#!/usr/bin/env python3
"""PipeWire-Pulse WebRTC echo cancellation and Pipecat input transport."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

from loguru import logger

from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams

AEC_SOURCE_NAME = os.environ.get("VOICE_ROUTER_AEC_SOURCE_NAME", "pipecat_aec_source")
AEC_SINK_NAME = os.environ.get("VOICE_ROUTER_AEC_SINK_NAME", "pipecat_aec_sink")
AEC_RATE = int(os.environ.get("VOICE_ROUTER_AEC_RATE", "48000"))
AEC_CHANNELS = int(os.environ.get("VOICE_ROUTER_AEC_CHANNELS", "1"))
MODULE_MARKER = f"source_name={AEC_SOURCE_NAME}"


def _pactl(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["pactl", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"pactl {' '.join(args)} failed")
    return result.stdout.strip()


def _short(kind: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in _pactl("list", "short", kind).splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            rows.append((fields[0], fields[1]))
    return rows


def _matching_module_ids() -> list[str]:
    ids: list[str] = []
    for line in _pactl("list", "short", "modules").splitlines():
        fields = line.split("\t", 2)
        if len(fields) >= 3 and fields[1] == "module-echo-cancel" and MODULE_MARKER in fields[2]:
            ids.append(fields[0])
    return ids


def _source_master() -> str:
    explicit = os.environ.get("VOICE_ROUTER_AEC_SOURCE_MASTER", "").strip()
    if explicit:
        return explicit
    sources = [name for _, name in _short("sources") if ".monitor" not in name and name != AEC_SOURCE_NAME]
    usb = next((name for name in sources if "usb" in name.lower()), None)
    if usb:
        return usb
    default = _pactl("get-default-source")
    if default and default != AEC_SOURCE_NAME:
        return default
    raise RuntimeError("no physical microphone source found for echo cancellation")


def _sink_master() -> str:
    explicit = os.environ.get("VOICE_ROUTER_AEC_SINK_MASTER", "").strip()
    if explicit:
        return explicit
    sinks = [name for _, name in _short("sinks") if name != AEC_SINK_NAME]
    hdmi = next((name for name in sinks if "hdmi" in name.lower()), None)
    if hdmi:
        return hdmi
    default = _pactl("get-default-sink")
    if default and default != AEC_SINK_NAME:
        return default
    raise RuntimeError("no physical playback sink found for echo cancellation")


def cleanup_echo_cancel() -> None:
    """Remove only the echo-cancel module owned by this voice router."""
    for module_id in _matching_module_ids():
        _pactl("unload-module", module_id, check=False)


def setup_echo_cancel() -> dict[str, str]:
    """Create a WebRTC AEC source/sink pair bound to the physical devices."""
    cleanup_echo_cancel()
    source_master = _source_master()
    sink_master = _sink_master()
    module_id = _pactl(
        "load-module",
        "module-echo-cancel",
        "aec_method=webrtc",
        f"source_master={source_master}",
        f"sink_master={sink_master}",
        f"source_name={AEC_SOURCE_NAME}",
        f"sink_name={AEC_SINK_NAME}",
        f"rate={AEC_RATE}",
        f"channels={AEC_CHANNELS}",
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sources = {name for _, name in _short("sources")}
        sinks = {name for _, name in _short("sinks")}
        if AEC_SOURCE_NAME in sources and AEC_SINK_NAME in sinks:
            logger.info(
                f"WebRTC AEC ready module={module_id} mic={source_master} playback={sink_master} "
                f"source={AEC_SOURCE_NAME} sink={AEC_SINK_NAME}"
            )
            return {
                "module_id": module_id,
                "source_master": source_master,
                "sink_master": sink_master,
                "source": AEC_SOURCE_NAME,
                "sink": AEC_SINK_NAME,
            }
        time.sleep(0.1)
    cleanup_echo_cancel()
    raise RuntimeError("echo-cancel module loaded but virtual source/sink did not appear")


class PulseAECInputTransport(BaseInputTransport):
    """Capture 20 ms PCM frames from PipeWire's echo-cancelled Pulse source."""

    def __init__(self, source: str = AEC_SOURCE_NAME):
        super().__init__(
            TransportParams(
                audio_in_enabled=True,
                audio_in_sample_rate=AEC_RATE,
                audio_in_channels=AEC_CHANNELS,
            )
        )
        self._source = source
        self._capture_process: asyncio.subprocess.Process | None = None
        self._capture_task: asyncio.Task | None = None
        self._stopping = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._capture_process:
            return
        self._stopping = False
        self._capture_process = await asyncio.create_subprocess_exec(
            "parec",
            "--raw",
            "--format=s16le",
            f"--rate={AEC_RATE}",
            f"--channels={AEC_CHANNELS}",
            f"--device={self._source}",
            "--latency-msec=40",
            "--process-time-msec=20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._capture_task = self.create_task(self._capture_loop())
        await self.set_transport_ready(frame)
        logger.info(f"capturing echo-cancelled mic from Pulse source {self._source}")

    async def stop(self, frame):
        await self._stop_capture()
        await super().stop(frame)

    async def cancel(self, frame):
        await self._stop_capture()
        await super().cancel(frame)

    async def cleanup(self):
        await self._stop_capture()
        await super().cleanup()

    async def _stop_capture(self):
        self._stopping = True
        if self._capture_task:
            await self.cancel_task(self._capture_task)
            self._capture_task = None
        if self._capture_process:
            if self._capture_process.returncode is None:
                self._capture_process.terminate()
                try:
                    await asyncio.wait_for(self._capture_process.wait(), timeout=2)
                except TimeoutError:
                    self._capture_process.kill()
                    await self._capture_process.wait()
            self._capture_process = None

    async def _capture_loop(self):
        process = self._capture_process
        if not process or not process.stdout:
            return
        frame_bytes = int(AEC_RATE * 0.02) * AEC_CHANNELS * 2
        try:
            while True:
                audio = await process.stdout.readexactly(frame_bytes)
                await self.push_audio_frame(
                    InputAudioRawFrame(audio=audio, sample_rate=AEC_RATE, num_channels=AEC_CHANNELS)
                )
        except asyncio.IncompleteReadError:
            if not self._stopping:
                logger.error(f"echo-cancelled microphone capture exited rc={process.returncode}")
                await self.push_error("PipeWire echo-cancelled microphone capture stopped", fatal=True)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "setup":
        print(setup_echo_cancel())
    elif command == "cleanup":
        cleanup_echo_cancel()
    elif command == "status":
        print(
            {
                "modules": _matching_module_ids(),
                "source": AEC_SOURCE_NAME in {name for _, name in _short("sources")},
                "sink": AEC_SINK_NAME in {name for _, name in _short("sinks")},
            }
        )
    else:
        raise SystemExit(f"usage: {argv[0]} setup|cleanup|status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

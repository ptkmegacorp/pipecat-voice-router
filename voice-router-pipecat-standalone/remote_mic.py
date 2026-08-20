#!/usr/bin/env python3
"""LAN WebSocket ingest for the A15 Saturn Mic → Pipecat / Pig.

Phone sends 16 kHz mono s16le PCM. Frames are upsampled to the 48 kHz / 20 ms
size PulseAEC already uses so Silero/Moonshine stay unchanged.

Does not touch phone-node-pi :8766/voice (llama/smart ledger).
"""

from __future__ import annotations

import asyncio
import audioop
import json
import os
from http import HTTPStatus

from loguru import logger
from websockets.asyncio.server import ServerConnection, serve

from pipecat.frames.frames import InputAudioRawFrame

from pulse_aec import AEC_CHANNELS, AEC_RATE, PulseAECInputTransport

REMOTE_MIC_HOST = os.environ.get("VOICE_ROUTER_REMOTE_MIC_HOST", "0.0.0.0")
REMOTE_MIC_PORT = int(os.environ.get("VOICE_ROUTER_REMOTE_MIC_PORT", "8789"))
PCM16K_FRAME = int(16000 * 0.02) * 2  # 640
PCM48K_FRAME = int(AEC_RATE * 0.02) * AEC_CHANNELS * 2  # 1920


class RemoteMicHub:
    def __init__(self, on_active=None):
        self._on_active = on_active
        self._q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._buf = bytearray()
        self._rate_state = None
        self._clients = 0
        self._server = None
        self.active = False

    def _set_active(self, on: bool) -> None:
        self.active = on
        if self._on_active:
            self._on_active(on)

    async def serve(self) -> None:
        self._server = await serve(
            self._handler,
            REMOTE_MIC_HOST,
            REMOTE_MIC_PORT,
            max_size=64 * 1024,
            process_request=self._check_path,
        )
        logger.info(f"saturn-mic listening ws://{REMOTE_MIC_HOST}:{REMOTE_MIC_PORT}/mic")
        await self._server.wait_closed()

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def get_48k(self) -> bytes:
        return await self._q.get()

    @staticmethod
    async def _check_path(connection: ServerConnection, request):
        path = (request.path or "/").split("?", 1)[0]
        if path in ("/mic", "/mic/"):
            return None
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    async def _handler(self, ws: ServerConnection) -> None:
        if self._clients > 0:
            await ws.close(1013, "busy")
            return
        self._clients = 1
        self._buf.clear()
        self._rate_state = None
        self._drain()
        self._set_active(True)
        logger.info("saturn-mic A15 connected")
        try:
            await ws.send(json.dumps({"type": "mic.ready"}))
            async for message in ws:
                if isinstance(message, str):
                    continue
                self._feed(message)
        except Exception as exc:
            logger.warning(f"saturn-mic socket: {exc}")
        finally:
            self._clients = 0
            self._buf.clear()
            self._rate_state = None
            self._drain()
            self._set_active(False)
            logger.info("saturn-mic A15 disconnected")

    def _feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        while len(self._buf) >= PCM16K_FRAME:
            chunk = bytes(self._buf[:PCM16K_FRAME])
            del self._buf[:PCM16K_FRAME]
            converted, self._rate_state = audioop.ratecv(
                chunk, 2, 1, 16000, AEC_RATE, self._rate_state
            )
            if len(converted) != PCM48K_FRAME:
                converted = (converted + bytes(PCM48K_FRAME))[:PCM48K_FRAME]
            try:
                self._q.put_nowait(converted)
            except asyncio.QueueFull:
                try:
                    self._q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._q.put_nowait(converted)
                except asyncio.QueueFull:
                    pass

    def _drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                return


class HybridMicInputTransport(PulseAECInputTransport):
    """Pulse AEC by default; A15 Saturn Mic replaces it while a client is connected."""

    def __init__(self, source: str, hub: RemoteMicHub):
        super().__init__(source)
        self._hub = hub

    async def _capture_loop(self):
        process = self._capture_process
        if not process or not process.stdout:
            return
        frame_bytes = PCM48K_FRAME
        silence = bytes(frame_bytes)
        pulse_task = None
        try:
            while True:
                if pulse_task is None:
                    pulse_task = asyncio.create_task(process.stdout.readexactly(frame_bytes))
                if self._hub.active:
                    try:
                        audio = await asyncio.wait_for(self._hub.get_48k(), timeout=0.02)
                    except TimeoutError:
                        audio = silence
                    if pulse_task.done():
                        exc = pulse_task.exception()
                        if exc is not None:
                            raise exc
                        pulse_task = None
                else:
                    audio = await pulse_task
                    pulse_task = None
                await self.push_audio_frame(
                    InputAudioRawFrame(audio=audio, sample_rate=AEC_RATE, num_channels=AEC_CHANNELS)
                )
        except asyncio.IncompleteReadError:
            if not self._stopping:
                logger.error(f"echo-cancelled microphone capture exited rc={process.returncode}")
                await self.push_error("PipeWire echo-cancelled microphone capture stopped", fatal=True)
        finally:
            if pulse_task is not None and not pulse_task.done():
                pulse_task.cancel()
                try:
                    await pulse_task
                except (asyncio.CancelledError, Exception):
                    pass

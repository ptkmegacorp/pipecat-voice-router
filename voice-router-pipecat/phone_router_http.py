#!/usr/bin/env python3
"""LAN HTTP ingest for Phone Saturn Mic — route transcripts, no Pipecat PCM."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from routing import route_text
from router_execute_phone import execute_routed

import voice_router as vr

HOST = os.environ.get("VOICE_ROUTER_PHONE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICE_ROUTER_PHONE_PORT", "8790"))


def _public_action(action: dict) -> dict:
    return {
        "name": action.get("name"),
        "route": action.get("route"),
        "function": action.get("function"),
        "args": action.get("args") or {},
        "tts": action.get("tts"),
        "text": action.get("text"),
        "match_method": action.get("match_method"),
        "match_score": action.get("match_score"),
        "match_phrase": action.get("match_phrase"),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("phone-voice-router " + (fmt % args), flush=True)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return ""
        return self.rfile.read(min(length, 65536)).decode("utf-8", "replace")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/health", "/status"}:
            self._send(
                200,
                {
                    "ok": True,
                    "service": "phone-voice-router",
                    "port": PORT,
                    "phone_tts": True,
                },
            )
            return
        self._send(404, {"ok": False, "error": f"unknown path {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in {"/route", "/say"}:
            self._send(404, {"ok": False, "error": f"unknown path {path}"})
            return
        raw = self._read_body()
        try:
            data: dict[str, Any] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(data, dict):
            self._send(400, {"ok": False, "error": "body must be a json object"})
            return
        text = str(data.get("text") or "").strip()
        if not text:
            self._send(400, {"ok": False, "error": 'body must include {"text": "..."}'})
            return

        print(f"phone-voice-router route text={text!r}", flush=True)
        ctx = {"focused_window": vr.get_focused_window()}
        action = route_text(text, ctx)
        try:
            speak = execute_routed(action)
        except Exception as exc:
            self._send(
                500,
                {
                    "ok": False,
                    "error": str(exc),
                    "routed": _public_action(action),
                    "speak": [],
                },
            )
            return
        ack = str(action.get("name") or action.get("function") or "ok")
        self._send(
            200,
            {
                "ok": True,
                "ack": ack,
                "routed": _public_action(action),
                "speak": speak,
            },
        )


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"phone-voice-router listening http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

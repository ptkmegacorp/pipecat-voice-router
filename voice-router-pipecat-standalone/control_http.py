"""Localhost control plane for the running Pipecat router.

Loopback HTTP plus a Unix socket. TTS on/off is one resource; the same
socket can also abort, inject a phrase through the voice router, list
routes, and toggle frontier — without restarting the mic stack.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any, Callable

from loguru import logger

CONTROL_HOST = os.environ.get("VOICE_ROUTER_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("VOICE_ROUTER_CONTROL_PORT", "8788"))
CONTROL_SOCK = Path(
    os.environ.get(
        "VOICE_ROUTER_CONTROL_SOCK",
        Path.home() / ".cache/pipecat-voice/control.sock",
    )
)
VOICE_STATUS_FILE = Path.home() / ".cache/pipecat-voice/status.json"

_hooks: "ControlHooks | None" = None
_servers: list[Any] = []


@dataclass
class ControlHooks:
    get_speaker: Callable[[], Any]
    execute: Callable[[dict], Any]
    route_text: Callable[[str], dict]
    abort: Callable[[], None]
    get_backend: Callable[[], str]
    routes: list[dict]


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ThreadingUnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _control_meta() -> dict:
    return {
        "http": f"http://{CONTROL_HOST}:{CONTROL_PORT}",
        "sock": str(CONTROL_SOCK),
    }


def _voice_status() -> dict:
    try:
        data = json.loads(VOICE_STATUS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _public_route(route: dict) -> dict:
    return {
        "name": route.get("name"),
        "description": route.get("description"),
        "function": route.get("function"),
        "route": route.get("route"),
        "args": route.get("args") or {},
        "match": route.get("match") or [],
        "prefix": route.get("prefix"),
        "tts": route.get("tts"),
    }


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


def _speaker_snapshot() -> dict:
    if _hooks is None:
        return {"ok": False, "error": "control not initialized"}
    speaker = _hooks.get_speaker()
    if speaker is None:
        return {"ok": False, "error": "tts speaker not initialized"}
    snap = speaker.snapshot()
    snap["ok"] = True
    snap["control"] = _control_meta()
    return snap


def _full_status() -> dict:
    if _hooks is None:
        return {"ok": False, "error": "control not initialized"}
    tts = _speaker_snapshot()
    tts.pop("control", None)
    tts.pop("ok", None)
    return {
        "ok": True,
        "pid": os.getpid(),
        "backend": _hooks.get_backend(),
        "voice": _voice_status(),
        "tts": tts,
        "route_count": len(_hooks.routes),
        "control": _control_meta(),
    }


def _parse_json_body(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return data if isinstance(data, dict) else {"_raw": raw}


def _parse_enabled(raw: str) -> bool | None:
    data = _parse_json_body(raw)
    if "enabled" in data:
        return bool(data["enabled"])
    text = str(data.get("_raw") or raw or "").strip().lower()
    if text in {"1", "true", "on", "enable", "enabled"}:
        return True
    if text in {"0", "false", "off", "disable", "disabled"}:
        return False
    return None


def _find_route(name: str) -> dict | None:
    if _hooks is None:
        return None
    needle = name.strip().lower()
    for route in _hooks.routes:
        if str(route.get("name") or "").lower() == needle:
            return route
        if str(route.get("function") or "").lower() == needle:
            return route
    return None


class ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("pipecat-control " + fmt, *args)

    def _send(self, code: int, payload: dict) -> None:
        body = _json_bytes(payload)
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
            snap = _full_status()
            self._send(200 if snap.get("ok") else 503, snap)
            return
        if path in {"/tts", "/tts/status"}:
            snap = _speaker_snapshot()
            self._send(200 if snap.get("ok") else 503, snap)
            return
        if path in {"/routes"}:
            if _hooks is None:
                self._send(503, {"ok": False, "error": "control not initialized"})
                return
            self._send(200, {"ok": True, "routes": [_public_route(r) for r in _hooks.routes]})
            return
        self._send(404, {"ok": False, "error": f"unknown path {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if _hooks is None:
            self._send(503, {"ok": False, "error": "control not initialized"})
            return
        speaker = _hooks.get_speaker()
        body = self._read_body()
        data = _parse_json_body(body)

        if path in {"/tts/stop", "/stop", "/interrupt"}:
            if speaker is None:
                self._send(503, {"ok": False, "error": "tts speaker not initialized"})
                return
            speaker.interrupt()
            snap = _speaker_snapshot()
            snap["action"] = "stop"
            self._send(200, snap)
            return

        if path in {"/abort"}:
            if speaker is not None:
                speaker.interrupt()
            try:
                _hooks.abort()
            except Exception as exc:
                logger.warning(f"pipecat-control abort failed: {exc}")
                self._send(500, {"ok": False, "error": str(exc)})
                return
            snap = _full_status()
            snap["action"] = "abort"
            self._send(200, snap)
            return

        if path in {"/tts", "/tts/on", "/tts/off", "/tts/enable", "/tts/disable"}:
            if speaker is None:
                self._send(503, {"ok": False, "error": "tts speaker not initialized"})
                return
            if path.endswith("/off") or path.endswith("/disable"):
                enabled = False
            elif path.endswith("/on") or path.endswith("/enable"):
                enabled = True
            else:
                parsed = _parse_enabled(body)
                if parsed is None:
                    self._send(400, {"ok": False, "error": 'body must be {"enabled": true|false}'})
                    return
                enabled = parsed
            speaker.set_enabled(enabled)
            logger.info(f"pipecat-control tts enabled={enabled}")
            snap = _speaker_snapshot()
            snap["action"] = "on" if enabled else "off"
            self._send(200, snap)
            return

        if path in {"/frontier", "/frontier/on", "/frontier/off"}:
            if path.endswith("/off"):
                enabled = False
            elif path.endswith("/on"):
                enabled = True
            else:
                parsed = _parse_enabled(body)
                if parsed is None:
                    self._send(400, {"ok": False, "error": 'body must be {"enabled": true|false}'})
                    return
                enabled = parsed
            action = {
                "name": "enable_frontier" if enabled else "disable_frontier",
                "route": "voice_control",
                "function": "set_frontier_mode",
                "args": {"backend": "frontier" if enabled else "local"},
                "tts": True,
                "text": "frontier on" if enabled else "frontier off",
            }
            try:
                _hooks.execute(action)
            except Exception as exc:
                logger.warning(f"pipecat-control frontier failed: {exc}")
                self._send(500, {"ok": False, "error": str(exc), "action": _public_action(action)})
                return
            snap = _full_status()
            snap["action"] = "frontier_on" if enabled else "frontier_off"
            snap["routed"] = _public_action(action)
            self._send(200, snap)
            return

        if path in {"/say", "/route"}:
            text = str(data.get("text") or data.get("_raw") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": 'body must be {"text": "..."}'})
                return
            action = _hooks.route_text(text)
            try:
                _hooks.execute(action)
            except Exception as exc:
                logger.warning(f"pipecat-control say failed: {exc}")
                self._send(500, {"ok": False, "error": str(exc), "routed": _public_action(action)})
                return
            snap = _full_status()
            snap["action"] = "say"
            snap["routed"] = _public_action(action)
            self._send(200, snap)
            return

        if path in {"/execute"}:
            name = str(data.get("name") or data.get("function") or "").strip()
            extra_args = data.get("args") if isinstance(data.get("args"), dict) else {}
            if not name:
                self._send(400, {"ok": False, "error": 'body must be {"name": "disable_tts"} or {"function": "..."}'})
                return
            route = _find_route(name)
            if route is None:
                self._send(404, {"ok": False, "error": f"unknown route {name}"})
                return
            action = {**route, "args": {**(route.get("args") or {}), **extra_args}, "text": data.get("text") or name}
            try:
                _hooks.execute(action)
            except Exception as exc:
                logger.warning(f"pipecat-control execute failed: {exc}")
                self._send(500, {"ok": False, "error": str(exc), "routed": _public_action(action)})
                return
            snap = _full_status()
            snap["action"] = "execute"
            snap["routed"] = _public_action(action)
            self._send(200, snap)
            return

        if path in {"/speak"}:
            text = str(data.get("text") or data.get("_raw") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": 'body must be {"text": "..."}'})
                return
            if speaker is None:
                self._send(503, {"ok": False, "error": "tts speaker not initialized"})
                return
            speaker.speak(text, force=bool(data.get("force", True)))
            snap = _speaker_snapshot()
            snap["action"] = "speak"
            snap["text"] = text
            self._send(200, snap)
            return

        self._send(404, {"ok": False, "error": f"unknown path {path}"})


def _serve(server: Any, label: str) -> None:
    logger.info(f"pipecat-control listening on {label}")
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception as exc:
        logger.warning(f"pipecat-control {label} stopped: {exc}")


def start_control(hooks: ControlHooks | Callable[[], Any]) -> None:
    """Start loopback HTTP + Unix socket control in daemon threads."""
    global _hooks
    if callable(hooks) and not isinstance(hooks, ControlHooks):
        # Backward compat: start_control(lambda: SPEAKER)
        _hooks = ControlHooks(
            get_speaker=hooks,
            execute=lambda _action: None,
            route_text=lambda text: {"text": text},
            abort=lambda: None,
            get_backend=lambda: "unknown",
            routes=[],
        )
    else:
        _hooks = hooks

    try:
        httpd = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
        threading.Thread(
            target=_serve, args=(httpd, f"{CONTROL_HOST}:{CONTROL_PORT}"), daemon=True
        ).start()
        _servers.append(httpd)
    except OSError as exc:
        logger.warning(f"pipecat-control HTTP bind {CONTROL_HOST}:{CONTROL_PORT} failed: {exc}")

    try:
        CONTROL_SOCK.parent.mkdir(parents=True, exist_ok=True)
        if CONTROL_SOCK.exists():
            CONTROL_SOCK.unlink()
        unix = ThreadingUnixHTTPServer(str(CONTROL_SOCK), ControlHandler)
        os.chmod(CONTROL_SOCK, 0o600)
        threading.Thread(target=_serve, args=(unix, str(CONTROL_SOCK)), daemon=True).start()
        _servers.append(unix)
    except OSError as exc:
        logger.warning(f"pipecat-control unix bind {CONTROL_SOCK} failed: {exc}")


def stop_control() -> None:
    for server in _servers:
        try:
            server.shutdown()
        except Exception:
            pass
    _servers.clear()
    CONTROL_SOCK.unlink(missing_ok=True)

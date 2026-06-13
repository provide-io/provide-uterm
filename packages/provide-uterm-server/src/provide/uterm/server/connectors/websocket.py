#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""WebSocket-backed connector for the hosted server app."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from typing import Any

from provide.telemetry import get_logger
from provide.uterm.screen import decode_cp437
from provide.uterm.server.connectors.base import SessionConnector

_COLS = 80
_ROWS = 25

logger = get_logger(__name__)


class WebSocketSessionConnector(SessionConnector):
    """Connect a hosted session to a remote WebSocket endpoint."""

    _VALID_CONFIG_KEYS: frozenset[str] = frozenset({"url", "input_mode", "hub_overlay"})

    def __init__(self, session_id: str, display_name: str, config: dict[str, Any]) -> None:
        unknown = set(config) - self._VALID_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown websocket connector_config keys: {sorted(unknown)}")
        self._session_id = session_id
        self._display_name = display_name
        self._url = str(config["url"])
        self._ws: Any | None = None
        self._connected = False
        self._input_mode = str(config.get("input_mode", "open"))
        self._hub_overlay: bool = bool(config.get("hub_overlay", True))
        self._paused = False
        self._received_bytes = 0
        self._screen_buffer = ""
        self._banner = f"Connecting to {self._url}"

    def _screen(self) -> str:
        if not self._hub_overlay:
            return self._screen_buffer
        header = [
            f"\x1b[1;35m[{self._display_name} ({self._session_id})]\x1b[0m",
            "-" * 60,
            f"\x1b[32mUpstream:\x1b[0m {self._url}",
            f"\x1b[32mMode:\x1b[0m {'Shared input' if self._input_mode == 'open' else 'Exclusive hijack'}",
            f"\x1b[32mControl:\x1b[0m {'Paused for hijack' if self._paused else 'Live'}",
            f"\x1b[33m{self._banner}\x1b[0m",
            "",
        ]
        lines = (header + self._screen_buffer.splitlines())[-_ROWS:]
        return "\n".join(lines)

    def _snapshot(self) -> dict[str, Any]:
        screen = self._screen()
        lines = screen.splitlines() or [""]
        last = lines[-1]
        return {
            "type": "snapshot",
            "screen": screen,
            "cursor": {"x": min(len(last), _COLS - 1), "y": min(len(lines) - 1, _ROWS - 1)},
            "cols": _COLS,
            "rows": _ROWS,
            "screen_hash": hashlib.sha256(screen.encode("utf-8")).hexdigest()[:16],
            "cursor_at_end": True,
            "has_trailing_space": False,
            "prompt_detected": {"prompt_id": "ws_stream"},
            "ts": time.time(),
        }

    def _hello(self) -> dict[str, Any]:
        from provide.uterm.shell.terminal._output import worker_hello as _worker_hello

        return _worker_hello(self._input_mode)

    async def start(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise ImportError(
                "websocket connector requires the 'websocket' extra: pip install 'provide-uterm[websocket]'"
            ) from exc

        ws = await websockets.connect(self._url)
        # M3 (DNS-rebinding) post-connect mitigation: the create-time egress
        # guard validated the resolved name, but a TTL-0 rebind could flip the
        # name to a metadata IP for this actual connect.  Validate the REAL
        # peer IP we reached (no second DNS lookup) before any application data
        # flows; the TLS handshake already completed using the original
        # hostname, so SNI / cert validation are untouched.  Abort on a blocked
        # peer.  Only metadata IPs are enforced here (always-on, no config); the
        # private-range block is not threaded to connectors.
        await self._assert_peer_allowed(ws)
        self._ws = ws
        self._connected = True
        self._banner = f"Connected to {self._url}"

    async def _assert_peer_allowed(self, ws: Any) -> None:
        """Run the actual connected peer IP through the egress metadata check.

        Closes *ws* and raises ``EgressBlockedError`` if the peer is a blocked
        target.  A missing peername (None) is treated as "proceed" — the
        create-time guard already validated the resolved name, and this check
        only ever ABORTS on a positively-identified blocked peer.
        """
        from provide.uterm.server.egress import assert_ip_allowed

        peer = ws.remote_address
        if not peer:
            return
        peer_ip = peer[0]
        try:
            assert_ip_allowed(str(peer_ip), block_private=False)
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()
            logger.warning(
                "websocket_connector_peer_blocked session_id=%s url=%s peer_ip=%s — aborting (DNS-rebind guard)",
                self._session_id,
                self._url,
                peer_ip,
            )
            raise

    async def stop(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def poll_messages(self) -> list[dict[str, Any]]:
        if not self.is_connected() or self._ws is None:
            return []
        try:
            data = await asyncio.wait_for(self._ws.recv(), timeout=0.1)
        except TimeoutError:
            return []
        except Exception:
            self._connected = False
            self._banner = "WebSocket connection closed."
            # ws is guaranteed non-None: guarded by is_connected() check above.
            ws, self._ws = self._ws, None
            with contextlib.suppress(Exception):
                await ws.close()
            return [self._snapshot()]

        if isinstance(data, bytes):
            text = decode_cp437(data)
            self._received_bytes += len(data)
        else:
            text = data
            self._received_bytes += len(data.encode("utf-8"))

        self._screen_buffer = (self._screen_buffer + text)[-32_000:]
        self._banner = f"Received {self._received_bytes} bytes from WebSocket upstream."
        return [{"type": "term", "data": text, "ts": time.time()}, self._snapshot()]

    async def handle_input(self, data: str) -> list[dict[str, Any]]:
        if self.is_connected() and self._ws is not None:
            await self._ws.send(data)
            self._banner = f"Sent {len(data)} characters upstream."
        return [self._snapshot()]

    async def handle_control(self, action: str) -> list[dict[str, Any]]:
        if action == "pause":
            self._paused = True
            self._banner = "Exclusive control active."
        elif action == "resume":
            self._paused = False
            self._banner = "Exclusive control released."
        elif action == "step":
            self._banner = "Step requested. Awaiting upstream output."
        else:
            self._banner = f"Ignored control action: {action}"
        return [self._snapshot()]

    async def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    async def get_analysis(self) -> str:
        return "\n".join(
            [
                f"[websocket session analysis — worker: {self._session_id}]",
                f"url: {self._url}",
                f"input_mode: {self._input_mode}",
                f"paused: {self._paused}",
                f"bytes_received: {self._received_bytes}",
                f"connected: {self.is_connected()}",
            ]
        )

    async def clear(self) -> list[dict[str, Any]]:
        self._screen_buffer = ""
        self._banner = "Screen buffer cleared."
        return [self._snapshot()]

    async def set_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode not in {"open", "hijack"}:
            raise ValueError(f"invalid mode: {mode}")
        self._input_mode = mode
        if mode == "open":
            self._paused = False
        self._banner = f"Input mode set to {'Shared input' if mode == 'open' else 'Exclusive hijack'}."
        return [self._hello(), self._snapshot()]


from provide.uterm.server.connectors.registry import register_connector  # noqa: E402

register_connector("websocket", WebSocketSessionConnector)

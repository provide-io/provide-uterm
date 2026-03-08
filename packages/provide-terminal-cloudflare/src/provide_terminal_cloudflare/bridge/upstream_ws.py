from __future__ import annotations

import asyncio
import json
import time
from typing import Any


class UpstreamWsBridge:
    """WebSocket connector with reconnect/backoff behavior for v1 upstream mode.

    .. note::
        Not yet wired to the Durable Object routing layer (``entry.py`` / ``session_runtime.py``).
        ``UpstreamConfig`` is parsed from env vars but the bridge is not instantiated.
        This class is kept here as the intended integration point for proxying the DO
        to an external worker WS endpoint.
    """

    def __init__(
        self,
        url: str,
        *,
        max_backoff_s: int = 5,
        heartbeat_s: int = 25,
        connect_timeout_ms: int = 3_000,
    ) -> None:
        self.url = url
        self.max_backoff_s = max(1, max_backoff_s)
        self.heartbeat_s = max(1, heartbeat_s)
        self.connect_timeout_s = max(0.1, connect_timeout_ms / 1000.0)
        self._stop = asyncio.Event()
        self._ws: Any | None = None

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()

    async def run(self, on_message: Any) -> None:
        try:
            import websockets
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("websockets dependency required for upstream bridge") from exc

        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=self.connect_timeout_s) as ws:
                    self._ws = ws
                    attempt = 0
                    last_ping = time.time()
                    while not self._stop.is_set():
                        if time.time() - last_ping > self.heartbeat_s:
                            await ws.send(json.dumps({"type": "ping", "ts": time.time()}))
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except TimeoutError:
                            continue
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(parsed, dict):
                            await on_message(parsed)
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = min(float(self.max_backoff_s), 0.25 * (2 ** (attempt - 1)))
                await asyncio.sleep(delay)
            finally:
                self._ws = None

    async def send_json(self, payload: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload, ensure_ascii=True))
            return True
        except Exception:
            return False

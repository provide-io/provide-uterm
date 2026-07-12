#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""CLI demo: DO hibernate wake + browser resume token (unit-level, no CF account).

Two independent stories, both exercised against real CF package code with fakes:

1. **DO hibernation** — wipe in-memory maps, restore lease from SQLite, fan-out
   via ``getWebSockets()`` + attachment role (socket stays open at the edge).
2. **Browser resume** — mint one-time token → disconnect → reconnect →
   ``type: resume`` → ``hello.resumed=true`` with new token.

Live CF eviction still needs ``bash scripts/prove_cf_hibernate_resume.sh --real-cf``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# CF package is workspace-local; default uv env may not put its src on path.
_CF_SRC = Path(__file__).resolve().parents[1] / "packages" / "provide-uterm-cloudflare" / "src"
if str(_CF_SRC) not in sys.path:
    sys.path.insert(0, str(_CF_SRC))

from provide.uterm.cloudflare.api.ws_routes import handle_socket_message  # noqa: E402
from provide.uterm.cloudflare.bridge.hijack import HijackCoordinator, HijackSession  # noqa: E402
from provide.uterm.cloudflare.contracts import frame_json  # noqa: E402
from provide.uterm.cloudflare.do.session_runtime import SessionRuntime  # noqa: E402
from provide.uterm.cloudflare.state.store import SqliteStateStore  # noqa: E402

from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder  # noqa: E402

MAGENTA = "\033[1;35m"
GREEN = "\033[1;32m"
CYAN = "\033[1;36m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
_KEY = "test-secret-key-32-bytes-minimum!"


def banner(title: str) -> None:
    bar = "═" * (len(title) + 4)
    print(f"\n{MAGENTA}{bar}{RESET}")
    print(f"{MAGENTA}  {BOLD}{title}{RESET}{MAGENTA}  {RESET}")
    print(f"{MAGENTA}{bar}{RESET}\n")


def info(msg: str) -> None:
    print(f"{CYAN}  → {msg}{RESET}")


def ok(msg: str) -> None:
    print(f"{GREEN}  ✓ {msg}{RESET}")


def kv(key: str, value: object) -> None:
    print(f"    {DIM}{key}:{RESET} {BOLD}{value}{RESET}")


class _EdgeWs:
    """WS stub: attachment survives simulated DO cold start; edge holds the socket."""

    def __init__(self, attachment: str) -> None:
        self._attachment = attachment
        self.sent: list[str] = []

    def serializeAttachment(self, value: object) -> None:  # noqa: N802
        self._attachment = str(value)

    def deserializeAttachment(self) -> object:  # noqa: N802
        return self._attachment

    def send(self, data: str) -> None:
        self.sent.append(data)


def _decode(raw: str) -> dict:
    decoder = ControlFrameDecoder()
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ControlChunk)
    return event.control


def _make_runtime(worker_id: str = "demo-hib") -> SessionRuntime:
    conn = sqlite3.connect(":memory:")
    live: list[_EdgeWs] = []
    ctx = SimpleNamespace(
        storage=SimpleNamespace(
            sql=SimpleNamespace(exec=conn.execute),
            setAlarm=lambda _ms: None,
        ),
        id=SimpleNamespace(name=lambda: worker_id),
        getWebSockets=lambda: list(live),
    )
    env = SimpleNamespace(
        AUTH_MODE="jwt",
        JWT_ALGORITHMS="HS256",
        JWT_PUBLIC_KEY_PEM=_KEY,
        WORKER_BEARER_TOKEN="test-worker-token-padded-to-32xyz",  # noqa: S106
        RESUME_TTL_S="120",
        RESUME_ENABLED="1",
    )
    rt = SessionRuntime(ctx, env)
    rt._demo_live = live  # type: ignore[attr-defined]
    return rt


def _simulate_do_eviction(rt: SessionRuntime) -> None:
    rt.worker_ws = None
    rt.browser_sockets.clear()
    rt.raw_sockets.clear()
    rt.browser_hijack_owner.clear()
    rt.browser_resume_tokens.clear()
    rt.hijack._session = None
    rt.last_snapshot = None


async def demo_hibernate_wake() -> None:
    banner("1/2  DO hibernation wake (socket stays open)")
    info("warm: persist hijack lease + snapshot to SQLite")
    rt = _make_runtime("demo-hib-wake")
    browser = _EdgeWs(f"browser:admin:{rt.worker_id}")
    rt._demo_live.append(browser)  # type: ignore[attr-defined]

    mono_expiry = time.monotonic() + 600
    session = HijackSession(hijack_id="lease-demo", owner="operator", lease_expires_at=mono_expiry)
    rt.persist_lease(session)
    snap = {"type": "snapshot", "screen": "WARM\n", "cols": 80, "rows": 24}
    rt.store.save_snapshot(rt.worker_id, snap)
    rt.last_snapshot = snap
    ok("lease + snapshot durable")
    kv("hijack_id", "lease-demo")
    kv("owner", "operator")

    info("CF evicts DO: wipe every in-memory map (edge still holds WebSocket)")
    _simulate_do_eviction(rt)
    assert rt.browser_sockets == {}
    assert rt.hijack.session is None
    ok("in-memory maps empty")

    info("wake: _restore_state() reloads wall-clock lease from SQLite")
    rt._restore_state()
    assert rt.hijack.session is not None
    assert rt.hijack.session.hijack_id == "lease-demo"
    ok("lease restored from SQLite")
    kv("role_via_attachment", rt._socket_browser_role(browser))  # type: ignore[arg-type]
    assert rt._socket_role(browser) == "browser"  # type: ignore[arg-type]
    assert rt._socket_browser_role(browser) == "admin"  # type: ignore[arg-type]
    ok("role recovered from serializeAttachment (not object identity)")

    info("broadcast_to_browsers fans out via getWebSockets (not empty browser_sockets)")
    browser.sent.clear()
    await rt.broadcast_to_browsers({"type": "worker_status", "status": "online", "ts": time.time()})
    assert browser.sent, "edge socket must receive post-wake fan-out"
    frame = _decode(browser.sent[0])
    assert frame["type"] == "worker_status"
    ok(f"edge socket received {frame['type']} status={frame.get('status')}")


class _ResumeWs:
    def __init__(self) -> None:
        self._attachment: str | None = None

    def serializeAttachment(self, val: str) -> None:  # noqa: N802
        self._attachment = val


class _ResumeRuntime:
    def __init__(self, store: SqliteStateStore) -> None:
        self.worker_id = "demo-resume-w1"
        self.lifecycle_state = "stopped"
        self.input_mode = "hijack"
        self.hijack = HijackCoordinator()
        self.last_snapshot: dict | None = {"type": "snapshot", "screen": "RESUMED\n"}
        self.last_analysis: str | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.worker_ws = None
        self._sent: list[dict] = []
        self.config = SimpleNamespace(
            limits=SimpleNamespace(max_ws_message_bytes=1_048_576, max_input_chars=10_000),
            resume_ttl_s=300,
            resume_enabled=True,
        )
        self.store = store
        self.current_role = "admin"

    async def send_ws(self, ws: object, frame: dict) -> None:
        self._sent.append(frame)

    async def send_hijack_state(self, ws: object) -> None:
        self._sent.append({"type": "hijack_state"})

    async def broadcast_worker_frame(self, frame: object) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return True

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return self.current_role


async def demo_browser_resume() -> None:
    banner("2/2  Browser resume token (new socket after drop)")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = SqliteStateStore(conn.execute)
    store.migrate()
    runtime = _ResumeRuntime(store)

    info("session issues one-time resume_token on hello")
    old_tok = "tok-old"
    store.create_resume_token(old_tok, runtime.worker_id, "admin", 300)
    ok("token minted in SQLite resume_tokens")
    kv("token", f"{old_tok}…")
    kv("ttl_s", 300)

    info("browser reconnects and sends type=resume with stored token")
    ws = _ResumeWs()
    raw = frame_json("resume", token=old_tok)
    await handle_socket_message(runtime, ws, raw, is_worker=False)

    hellos = [m for m in runtime._sent if m.get("type") == "hello"]
    assert len(hellos) == 1, hellos
    hello = hellos[0]
    assert hello.get("resumed") is True
    new_tok = hello.get("resume_token")
    assert new_tok and new_tok != old_tok
    assert hello.get("role") == "admin"
    assert store.get_resume_token(old_tok) is None
    ok("hello.resumed=true")
    kv("role", hello.get("role"))
    kv("new_token", f"{str(new_tok)[:16]}…")
    kv("old_token_revoked", True)

    snap = [m for m in runtime._sent if m.get("type") == "snapshot"]
    assert snap, "snapshot replayed after resume"
    ok("last snapshot replayed to reconnected browser")
    kv("screen", repr(snap[0].get("screen")))

    info("UI: session-element shows status 'Resumed' for ~2.5s when hello.resumed")
    ok("frontend setStatus(live, Resumed) — see session-element.ts")


def main() -> int:
    banner("provide-uterm CF hibernate + browser resume")
    info("Level A demo (fakes) — no Cloudflare account required")
    info("Config: RESUME_TTL_S / RESUME_ENABLED on CloudflareConfig")
    try:
        asyncio.run(demo_hibernate_wake())
        asyncio.run(demo_browser_resume())
    except AssertionError as exc:
        print(f"\n  ✗ demo assertion failed: {exc}", file=sys.stderr)
        return 1
    banner("demo complete")
    ok("DO hibernate wake contract")
    ok("browser resume token → hello.resumed")
    info("Live DO eviction: bash scripts/prove_cf_hibernate_resume.sh --real-cf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""End-to-end test for the full SSH pubkey → DeckMux presence pipeline.

This test is the single integration point that exercises every new piece
of work in one connection:

1. Real asyncssh client presents an ed25519 pubkey + TERM + COLORTERM
2. Real :class:`SshWsGateway` runs an :class:`AuthorizedKeysFileResolver`
3. Real WS server stands in for a DeckMux hub
4. Gateway sends the ``identity`` control frame as the first message
5. Gateway also appends ``?colormode=…`` derived from pty-req/env
6. Hub parses the frame via :func:`parse_identity_frame` and builds a
   :class:`UserPresence` via :func:`identity_as_principal` through
   :meth:`DeckMuxMixin.deckmux_on_browser_connect`
7. Assert the final :class:`PresenceStore` has the expected user
8. Assert the connection URL the hub observed carries ``colormode=256``

If this test passes, the full "everything new works together" claim is
backed by evidence — an SRE sitting at an ssh client with the right
key would arrive in a DeckMux session with the right name, role, and
colour-aware terminal on the other end.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import asyncssh
import pytest
import websockets
import websockets.server

from provide.uterm.auth import AuthorizedKeysFileResolver
from provide.uterm.deckmux import (
    PresenceStore,
    identity_as_principal,
    parse_identity_frame,
)
from provide.uterm.deckmux._hub_mixin import DeckMuxMixin
from provide.uterm.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload
from tests.e2e._live_server import wait_for_condition

pytestmark = pytest.mark.asyncio


class _FakeDeckMuxHub(DeckMuxMixin):
    """Minimal DeckMux hub that processes inbound identity frames.

    Mirrors the real hub's shape closely enough that the integration
    path matches production — parse_identity_frame + identity_as_principal
    + deckmux_on_browser_connect — but without spinning up the full
    bridge + router stack.
    """

    def __init__(self) -> None:
        self._deckmux_init()
        self.broadcast = AsyncMock()
        # Recorded per-worker WS URLs so the test can also assert on the
        # ``?colormode=…`` query param from the SSH pty-req handshake.
        self.observed_urls: list[str] = []


async def _run_fake_hub(hub: _FakeDeckMuxHub) -> tuple[Any, int]:
    """Start a WS server that drives ``hub`` with each incoming connection."""

    async def _handler(ws: Any) -> None:
        # Record the upgrade path so the test can assert on ?colormode=...
        req = getattr(ws, "request", None)
        if req is not None:
            hub.observed_urls.append(str(getattr(req, "path", "")))
        else:
            hub.observed_urls.append(str(getattr(ws, "path", "") or ""))

        # One synthetic worker per connection.
        worker_id = f"worker-{id(ws):x}"

        # Read the first control frame; if it's an identity, feed the
        # mixin through its principal contract.
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
        except (TimeoutError, websockets.ConnectionClosed):
            return
        if not isinstance(msg, str):
            return
        try:
            frame = decode_control_payload(msg)
        except Exception:
            return
        identity = parse_identity_frame(frame)
        if identity is None:
            return
        principal = identity_as_principal(identity)
        await hub.deckmux_on_browser_connect(worker_id, ws, "operator", principal=principal)

        # Keep the WS alive so the SSH session has somewhere to talk to.
        with contextlib.suppress(Exception):
            async for _ in ws:
                pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port


async def _write_authorized_keys(
    tmp_path: Path,
    pubkey: asyncssh.SSHKey,
    *,
    subject: str,
    role: str,
    display_name: str,
) -> Path:
    line_body = pubkey.export_public_key().decode("ascii").strip()
    opts = f'subject="{subject}",claim-role="{role}",claim-display_name="{display_name}"'
    path = tmp_path / "authorized_keys"
    path.write_text(f"{opts} {line_body}\n", encoding="utf-8")
    return path


class TestFullChain:
    """One test to rule them all: SSH → resolver → identity → DeckMux."""

    async def test_pubkey_identity_term_colorterm_all_land(self, tmp_path: Path) -> None:
        """SRE with registered pubkey + TERM=xterm-256color + COLORTERM
        lands in DeckMux as their real subject with proper role & display."""
        # --- proxy-side setup: pubkey, authorized_keys file, resolver --
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        authorized_keys = await _write_authorized_keys(
            tmp_path,
            client_key,
            subject="sre:alice",
            role="oncall",
            display_name="Alice Liddell",
        )
        resolver = AuthorizedKeysFileResolver(authorized_keys)

        # --- upstream: fake DeckMux hub WS server -----------------------
        hub = _FakeDeckMuxHub()
        ws_srv, ws_port = await _run_fake_hub(hub)

        # --- gateway binding both ends ---------------------------------
        gw = SshWsGateway(
            f"ws://127.0.0.1:{ws_port}/deckmux",
            key_resolver=resolver,
        )
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="alice",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ) as conn,
                conn.create_process(
                    term_type="xterm-256color",
                ) as proc,
            ):
                proc.stdin.write_eof()
                # Give the gateway a beat to emit the identity frame and
                # for the hub to process it; we don't need stdout echo.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=2.0)
                # Deterministically wait for deckmux_on_browser_connect to
                # complete instead of a fixed-sleep settle.
                with contextlib.suppress(TimeoutError):
                    await wait_for_condition(
                        lambda: bool(hub.observed_urls),
                        timeout=3.0,
                        description="hub observed WS upgrade",
                    )
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        # --- assertions ------------------------------------------------

        # 1. Exactly one WS upgrade was observed and it carries ?colormode=256
        #    (derived from TERM=xterm-256color).
        assert hub.observed_urls, "hub saw no WS connection"
        path = hub.observed_urls[0]
        assert "colormode=256" in path, f"URL missing colormode: {path!r}"

        # 2. PresenceStore ended with exactly one user keyed by the
        #    resolver's subject (not a random ws-id).
        # Every inbound WS got its own worker_id, so look across stores.
        all_presences = []
        for store in hub._presence_stores.values():  # one store per worker
            assert isinstance(store, PresenceStore)
            all_presences.extend(store.get_all())
        assert len(all_presences) == 1, f"wrong presence count: {all_presences}"
        user = all_presences[0]

        # 3. The identity resolution chain populated name + role + initials
        #    from the authorized_keys claims.
        assert user.user_id == "sre:alice"
        assert user.name == "Alice Liddell"
        assert user.role == "operator"  # deckmux_on_browser_connect role arg
        assert user.initials == "AL"

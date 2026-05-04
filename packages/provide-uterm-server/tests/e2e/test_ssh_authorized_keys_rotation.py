#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""End-to-end tests: authorized_keys file mutated while the gateway is running.

AuthorizedKeysFileResolver reads the file on every resolve call (lazy re-read
by design). These four tests validate that property through real SSH connections:

1. Added-after-start  — key added while gateway runs → new connection resolves it.
2. Removed-after-start — key removed while gateway runs → next connection is rejected.
3. Modified-subject   — subject rewritten → next connection sees the new subject.
4. Mid-session removal is NOT honoured — live sessions survive a key removal.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import websockets
import websockets.server

from provide.terminal.auth import AuthorizedKeysFileResolver
from provide.terminal.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _write_authorized_keys(path: Path, pubkey: asyncssh.SSHKey, *, subject: str, role: str) -> None:
    """Overwrite *path* with one authorized_keys entry for *pubkey*."""
    line_body = pubkey.export_public_key().decode("ascii").strip()
    opts = f'subject="{subject}",claim-role="{role}"'
    path.write_text(f"{opts} {line_body}\n", encoding="utf-8")


async def _capture_ws_frames() -> tuple[Any, int, list[dict[str, Any]]]:
    """Minimal WS server that decodes every text frame as a control payload."""
    captured: list[dict[str, Any]] = []

    async def _handler(ws: Any) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    with contextlib.suppress(Exception):
                        captured.append(decode_control_payload(msg))
        except websockets.ConnectionClosed:
            pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port, captured


async def _ssh_connect_pubkey(
    ssh_port: int,
    key: asyncssh.SSHKey,
    *,
    username: str = "user",
) -> bool:
    """Try a pubkey SSH connection and return True if an identity frame was emitted.

    The upstream WS capture list is external; this helper just opens the SSH
    session long enough for the gateway to emit frames, then closes cleanly.
    """
    try:
        async with (
            asyncssh.connect(
                "127.0.0.1",
                port=ssh_port,
                known_hosts=None,
                username=username,
                client_keys=[key],
                preferred_auth="publickey",
                agent_path=None,
                config=[],
            ) as conn,
            conn.create_process() as proc,
        ):
            proc.stdin.write_eof()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.stdout.read(4096), timeout=2.0)
        return True
    except (asyncssh.PermissionDenied, asyncssh.DisconnectError):
        return False


# ---------------------------------------------------------------------------
# Test 1: key added after gateway starts → resolver picks it up
# ---------------------------------------------------------------------------


class TestAddedAfterStart:
    async def test_key_added_to_empty_file_is_accepted_on_next_connection(self, tmp_path: Path) -> None:
        """Gateway starts with an EMPTY authorized_keys file.

        First SSH attempt is rejected (require_resolver=True, no key known).
        We then write the client key into the file.
        Second SSH attempt succeeds and an identity frame is emitted — proving
        the resolver re-read the file without a gateway restart.
        """
        ak_path = tmp_path / "authorized_keys"
        ak_path.write_text("", encoding="utf-8")
        client_key = asyncssh.generate_private_key("ssh-ed25519")

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, captured = await _capture_ws_frames()
        gw = SshWsGateway(
            f"ws://127.0.0.1:{ws_port}",
            key_resolver=resolver,
            require_resolver=True,
        )
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            # First attempt — key is not in the file → expect rejection.
            with pytest.raises((asyncssh.PermissionDenied, asyncssh.DisconnectError)):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="alice",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ):
                    pass

            # Mutate the file — add the key (no gateway restart).
            _write_authorized_keys(ak_path, client_key, subject="sre:alice", role="oncall")

            # Second attempt — key is now in the file → should succeed.
            connected = await _ssh_connect_pubkey(ssh_port, client_key, username="alice")
            assert connected, "Expected connection to succeed after key was added"

            # Give the gateway time to emit the identity frame.
            await asyncio.sleep(0.2)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        identity_frames = [c for c in captured if c.get("type") == "identity"]
        assert identity_frames, f"No identity frame received after key addition; captured={captured!r}"
        assert identity_frames[0]["subject"] == "sre:alice"


# ---------------------------------------------------------------------------
# Test 2: key removed after gateway starts → rejected on next connection
# ---------------------------------------------------------------------------


class TestRemovedAfterStart:
    async def test_key_removed_from_file_is_rejected_on_next_connection(self, tmp_path: Path) -> None:
        """Gateway starts with one key in authorized_keys.

        First SSH attempt succeeds and an identity frame is emitted.
        We then overwrite the file to be empty (key removed).
        Second SSH attempt with the SAME key is rejected — proving the
        resolver re-read the file and no longer recognises the key.
        """
        ak_path = tmp_path / "authorized_keys"
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        _write_authorized_keys(ak_path, client_key, subject="sre:bob", role="admin")

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, captured = await _capture_ws_frames()
        gw = SshWsGateway(
            f"ws://127.0.0.1:{ws_port}",
            key_resolver=resolver,
            require_resolver=True,
        )
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            # First attempt — key is in the file → should succeed.
            connected = await _ssh_connect_pubkey(ssh_port, client_key, username="bob")
            assert connected, "Expected first connection to succeed (key registered)"
            await asyncio.sleep(0.1)

            identity_frames_before = [c for c in captured if c.get("type") == "identity"]
            assert identity_frames_before, "No identity frame on first (registered) connection"

            # Mutate the file — remove the key (no gateway restart).
            ak_path.write_text("", encoding="utf-8")

            # Second attempt — key is no longer in the file → expect rejection.
            with pytest.raises((asyncssh.PermissionDenied, asyncssh.DisconnectError)):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="bob",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ):
                    pass
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()


# ---------------------------------------------------------------------------
# Test 3: subject changed in file → next connection sees updated subject
# ---------------------------------------------------------------------------


class TestModifiedSubject:
    async def test_updated_subject_in_file_is_reflected_on_next_connection(self, tmp_path: Path) -> None:
        """Gateway starts with subject="old-subject".

        First SSH connection → identity frame carries subject="old-subject".
        We rewrite the file so the same key has subject="new-subject".
        Second SSH connection → identity frame carries subject="new-subject".
        This proves subject updates take effect on new connections without a
        gateway restart (and without affecting mid-session behaviour).
        """
        ak_path = tmp_path / "authorized_keys"
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        _write_authorized_keys(ak_path, client_key, subject="sre:old-subject", role="viewer")

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, captured = await _capture_ws_frames()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            # First connection — expect old subject.
            await _ssh_connect_pubkey(ssh_port, client_key, username="carol")
            await asyncio.sleep(0.2)

            frames_before = [c for c in captured if c.get("type") == "identity"]
            assert frames_before, "No identity frame on first connection"
            assert frames_before[0]["subject"] == "sre:old-subject", (
                f"Unexpected subject: {frames_before[0]['subject']!r}"
            )

            # Mutate the file — change the subject (no gateway restart).
            _write_authorized_keys(ak_path, client_key, subject="sre:new-subject", role="viewer")

            # Second connection — expect new subject.
            await _ssh_connect_pubkey(ssh_port, client_key, username="carol")
            await asyncio.sleep(0.2)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        frames_after = [c for c in captured if c.get("type") == "identity"]
        assert len(frames_after) >= 2, f"Expected at least 2 identity frames; got {frames_after!r}"
        # The last frame should carry the updated subject.
        last_subject = frames_after[-1]["subject"]
        assert last_subject == "sre:new-subject", f"Subject not updated after file mutation; got {last_subject!r}"


# ---------------------------------------------------------------------------
# Test 4: mid-session removal does NOT disconnect live connections (negative)
# ---------------------------------------------------------------------------


class TestMidSessionRemoval:
    async def test_live_session_survives_key_removal_from_file(self, tmp_path: Path) -> None:
        """Key is removed from authorized_keys WHILE a session is active.

        The in-flight SSH session must continue — removals only affect new
        connections, not live ones.  We confirm by asserting the session
        can still communicate (write_eof does not raise) after the key is
        removed.
        """
        ak_path = tmp_path / "authorized_keys"
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        _write_authorized_keys(ak_path, client_key, subject="sre:dave", role="operator")

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, _captured = await _capture_ws_frames()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        session_survived = False
        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="dave",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                # Session is live — now remove the key from the file.
                ak_path.write_text("", encoding="utf-8")

                # Give the gateway a moment to notice (it won't — by design).
                await asyncio.sleep(0.3)

                # The session should still be alive; write_eof should not raise.
                proc.stdin.write_eof()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=1.5)
                session_survived = True
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert session_survived, (
            "Live SSH session was terminated after key removal from authorized_keys — "
            "removals should only affect NEW connections, not in-flight ones."
        )

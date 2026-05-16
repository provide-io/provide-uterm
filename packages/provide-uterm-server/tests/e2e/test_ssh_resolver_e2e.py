#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Live e2e: SSH client with pubkey → resolver → identity hello frame on WS.

Spins up a real asyncssh server via :class:`SshWsGateway` with a configured
resolver, connects as a real SSH client presenting a real ed25519 key,
and asserts that the upstream WebSocket receives an ``identity`` control
frame as its first message with the resolved subject/claims.
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

from provide.uterm.auth import AuthorizedKeysFileResolver, ResolvedIdentity
from provide.uterm.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio


async def _capture_first_control_frame() -> tuple[Any, int, list[dict[str, Any]]]:
    """WS server that decodes the first message as a control frame."""
    captured: list[dict[str, Any]] = []

    async def _handler(ws: Any) -> None:
        # Decode every text frame as a control payload; ignore garbage.
        # Mirrors the pattern in test_process_handler_resumes_after_token_received.
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


async def _write_authorized_keys(tmp_path: Path, pubkey: asyncssh.SSHKey, subject: str, role: str) -> Path:
    """Write an authorized_keys file with the given pubkey + subject/role metadata."""
    line = pubkey.export_public_key().decode("ascii").strip()
    opts = f'subject="{subject}",claim-role="{role}"'
    path = tmp_path / "authorized_keys"
    path.write_text(f"{opts} {line}\n", encoding="utf-8")
    return path


class TestResolverE2E:
    async def test_registered_key_emits_identity_frame(self, tmp_path: Path) -> None:
        """Real SSH client with registered key → upstream WS receives identity frame."""
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        authorized_keys = await _write_authorized_keys(tmp_path, client_key, subject="sre:alice", role="oncall")
        # Sanity-check the file contents + fingerprint round-trip so any
        # test-infrastructure bug surfaces before we look at gateway wiring.
        expected_fp = client_key.get_fingerprint("sha256")
        direct = AuthorizedKeysFileResolver(authorized_keys)
        sanity = await direct.resolve(expected_fp, pubkey_blob=b"", username="alice")
        assert sanity is not None, (
            f"test fixture broken — resolver can't find {expected_fp} in {authorized_keys.read_text()!r}"
        )

        resolver = AuthorizedKeysFileResolver(authorized_keys)
        ws_srv, ws_port, captured = await _capture_first_control_frame()

        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
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
                    # Skip password — only pubkey should succeed.
                    preferred_auth="publickey",
                    # Prevent asyncssh from offering keys from ~/.ssh or the
                    # SSH agent ahead of client_keys: some builds probe both
                    # first, which would cause the resolver to see an
                    # unexpected fingerprint before client_keys is reached.
                    agent_path=None,
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                proc.stdin.write_eof()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=2.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert captured, "upstream WS received no control frame"
        identity_frames = [c for c in captured if c.get("type") == "identity"]
        assert identity_frames, f"no identity frame in {captured!r}"
        frame = identity_frames[0]
        assert frame["subject"] == "sre:alice"
        assert frame["claims"] == {"role": "oncall"}
        assert frame["transport"] == "ssh"
        assert frame["version"] == 1
        assert frame["fingerprint"].startswith("SHA256:")

    async def test_unregistered_key_no_identity_frame_emitted(self, tmp_path: Path) -> None:
        """Unknown key → resolver returns None → no identity frame sent.

        Uses require_resolver=False so the unknown key falls through to
        password auth (which we then satisfy to let the SSH session live
        long enough to confirm NO identity frame was emitted).
        """
        # Authorized file contains a DIFFERENT key from the one the client will use.
        registered_key = asyncssh.generate_private_key("ssh-ed25519")
        await _write_authorized_keys(tmp_path, registered_key, subject="sre:someone-else", role="admin")
        resolver = AuthorizedKeysFileResolver(tmp_path / "authorized_keys")

        client_key = asyncssh.generate_private_key("ssh-ed25519")

        ws_srv, ws_port, captured = await _capture_first_control_frame()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="bob",
                    client_keys=[client_key],
                    # Allow password fallback (default behaviour).
                    password="anything",
                    agent_path=None,
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                proc.stdin.write_eof()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=2.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        # No identity frame — unknown key falls through, password auth has
        # no resolver involvement.
        identity_frames = [c for c in captured if c.get("type") == "identity"]
        assert identity_frames == []

    async def test_require_resolver_rejects_unregistered_key(self, tmp_path: Path) -> None:
        """require_resolver=True → unknown key is rejected; no SSH session opens."""
        registered_key = asyncssh.generate_private_key("ssh-ed25519")
        await _write_authorized_keys(tmp_path, registered_key, subject="sre:someone", role="admin")
        resolver = AuthorizedKeysFileResolver(tmp_path / "authorized_keys")

        client_key = asyncssh.generate_private_key("ssh-ed25519")

        ws_srv, ws_port, _captured = await _capture_first_control_frame()
        gw = SshWsGateway(
            f"ws://127.0.0.1:{ws_port}",
            key_resolver=resolver,
            require_resolver=True,
        )
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            with pytest.raises((asyncssh.PermissionDenied, asyncssh.DisconnectError)):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="mallory",
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

    async def test_resolver_receives_correct_fingerprint(self, tmp_path: Path) -> None:
        """The gateway forwards the asyncssh-reported fingerprint to the resolver."""
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        expected_fp = client_key.get_fingerprint("sha256")

        seen: list[str] = []

        class _CapturingResolver:
            async def resolve(
                self,
                fingerprint: str,
                *,
                pubkey_blob: bytes,
                username: str,
            ) -> ResolvedIdentity | None:
                seen.append(fingerprint)
                return None  # no-op; just capture the fingerprint

        ws_srv, ws_port, _captured = await _capture_first_control_frame()
        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=_CapturingResolver())
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            with contextlib.suppress(Exception):
                async with asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="alice",
                    client_keys=[client_key],
                    password="x",  # fallback after resolver miss
                    agent_path=None,
                    config=[],
                ) as conn:
                    async with conn.create_process() as proc:
                        proc.stdin.write_eof()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(proc.stdout.read(4096), timeout=1.0)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        assert seen, "resolver never called"
        assert seen[0] == expected_fp

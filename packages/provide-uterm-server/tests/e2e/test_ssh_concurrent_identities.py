#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #2 coverage: concurrent SSH clients with distinct pubkeys get distinct identity frames.

Two (and then N=5) concurrent SSH clients each present a different ed25519 key.
Every key is listed in a shared authorized_keys file with a unique subject and
claim-role.  The test verifies that the upstream WS server sees one identity
frame per connection, that each frame carries the *correct* subject/role for its
key, and that fingerprints are not mixed up between sessions.
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

from provide.uterm.auth import AuthorizedKeysFileResolver
from provide.uterm.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key_fingerprint(key: asyncssh.SSHKey) -> str:
    return key.get_fingerprint("sha256")


def _pubkey_line(key: asyncssh.SSHKey, *, subject: str, role: str) -> str:
    body = key.export_public_key().decode("ascii").strip()
    opts = f'subject="{subject}",claim-role="{role}"'
    return f"{opts} {body}"


def _write_authorized_keys(tmp_path: Path, entries: list[tuple[asyncssh.SSHKey, str, str]]) -> Path:
    """Write an authorized_keys file with multiple entries.

    *entries* is a list of (key, subject, role) tuples.  All keys are written
    as separate lines in the same file so one resolver serves all of them.
    """
    lines = [_pubkey_line(k, subject=s, role=r) for k, s, r in entries]
    path = tmp_path / "authorized_keys"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _start_recording_ws_server() -> tuple[Any, int, list[dict[str, Any]]]:
    """WS server that records, per connection, the first identity frame received.

    Returns (server, port, frames_list).  Each element in *frames_list* is the
    decoded control payload dict from whichever connection produced it.
    """
    frames: list[dict[str, Any]] = []

    async def _handler(ws: Any) -> None:
        try:
            async for msg in ws:
                if not isinstance(msg, str):
                    continue
                with contextlib.suppress(Exception):
                    frame = decode_control_payload(msg)
                    if frame.get("type") == "identity":
                        frames.append(frame)
                        return  # one frame per connection is enough
        except Exception:
            pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port, frames


async def _open_session_and_close(
    ssh_port: int,
    key: asyncssh.SSHKey,
    username: str,
) -> None:
    """Open one SSH session (pubkey-only), flush stdin, and close."""
    with contextlib.suppress(Exception):
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrentIdentitiesDistinct:
    """Two concurrent SSH clients with different keys land at distinct identity frames."""

    async def test_two_distinct_keys_get_distinct_identity_frames(self, tmp_path: Path) -> None:
        """Alice and Bob connect simultaneously; each WS connection sees its own identity."""
        # Generate two distinct ed25519 key pairs.
        alice_key = asyncssh.generate_private_key("ssh-ed25519")
        bob_key = asyncssh.generate_private_key("ssh-ed25519")

        alice_fp = _key_fingerprint(alice_key)
        bob_fp = _key_fingerprint(bob_key)
        assert alice_fp != bob_fp, "test fixture error: keys are identical"

        # One authorized_keys file containing both entries.
        ak_path = _write_authorized_keys(
            tmp_path,
            [
                (alice_key, "sre:alice", "oncall"),
                (bob_key, "sre:bob", "viewer"),
            ],
        )

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, frames = await _start_recording_ws_server()

        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            # Both sessions run concurrently with their own distinct keys.
            await asyncio.gather(
                _open_session_and_close(ssh_port, alice_key, "alice"),
                _open_session_and_close(ssh_port, bob_key, "bob"),
            )
            # Short settle to let WS handler finish recording.
            await asyncio.sleep(0.2)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        # --- assertions -------------------------------------------------

        assert len(frames) == 2, f"expected 2 identity frames, got {len(frames)}: {frames!r}"

        # Index frames by subject for deterministic assertions regardless of
        # arrival order.
        by_subject = {f["subject"]: f for f in frames}

        assert "sre:alice" in by_subject, f"Alice's identity frame missing; got subjects: {list(by_subject)}"
        assert "sre:bob" in by_subject, f"Bob's identity frame missing; got subjects: {list(by_subject)}"

        alice_frame = by_subject["sre:alice"]
        bob_frame = by_subject["sre:bob"]

        # Fingerprints must match the client keys — no mix-up.
        assert alice_frame["fingerprint"] == alice_fp, (
            f"Alice fingerprint mismatch: frame={alice_frame['fingerprint']!r} key={alice_fp!r}"
        )
        assert bob_frame["fingerprint"] == bob_fp, (
            f"Bob fingerprint mismatch: frame={bob_frame['fingerprint']!r} key={bob_fp!r}"
        )

        # Claims (role) must be per-key, not shared or swapped.
        assert alice_frame.get("claims", {}).get("role") == "oncall", f"Alice role wrong: {alice_frame.get('claims')!r}"
        assert bob_frame.get("claims", {}).get("role") == "viewer", f"Bob role wrong: {bob_frame.get('claims')!r}"

        # Transport and version sanity.
        for frame in frames:
            assert frame.get("transport") == "ssh", f"unexpected transport in {frame!r}"
            assert frame.get("version") == 1, f"unexpected version in {frame!r}"


class TestFiveConcurrentIdentities:
    """N=5 concurrent SSH clients, each with a distinct key, all arrive with correct identity."""

    async def test_five_distinct_keys_all_arrive_correctly(self, tmp_path: Path) -> None:
        """Five concurrent sessions each land with a unique subject and the right fingerprint."""
        n = 5
        names = [f"user{i}" for i in range(n)]
        subjects = [f"sre:{name}" for name in names]
        roles = [f"role-{i}" for i in range(n)]

        keys = [asyncssh.generate_private_key("ssh-ed25519") for _ in range(n)]
        fingerprints = [_key_fingerprint(k) for k in keys]

        # Sanity: all fingerprints must be unique.
        assert len(set(fingerprints)) == n, "test fixture error: duplicate fingerprints"

        ak_path = _write_authorized_keys(
            tmp_path,
            list(zip(keys, subjects, roles)),
        )

        resolver = AuthorizedKeysFileResolver(ak_path)
        ws_srv, ws_port, frames = await _start_recording_ws_server()

        gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            await asyncio.gather(*[_open_session_and_close(ssh_port, keys[i], names[i]) for i in range(n)])
            await asyncio.sleep(0.3)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        # --- assertions -------------------------------------------------

        assert len(frames) == n, f"expected {n} identity frames, got {len(frames)}: {frames!r}"

        # All subjects must be unique (no duplication / cross-talk).
        received_subjects = [f["subject"] for f in frames]
        assert len(set(received_subjects)) == n, f"duplicate subjects detected (cross-talk?): {received_subjects!r}"
        assert set(received_subjects) == set(subjects), (
            f"subject set mismatch: got {set(received_subjects)!r}, want {set(subjects)!r}"
        )

        # Build lookup by subject for per-key assertions.
        by_subject = {f["subject"]: f for f in frames}

        for i in range(n):
            frame = by_subject[subjects[i]]
            assert frame["fingerprint"] == fingerprints[i], (
                f"{subjects[i]}: fingerprint mismatch frame={frame['fingerprint']!r} key={fingerprints[i]!r}"
            )
            assert frame.get("claims", {}).get("role") == roles[i], (
                f"{subjects[i]}: role mismatch: {frame.get('claims')!r}"
            )

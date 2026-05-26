#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #7: identity frame survives the full wire pipeline for non-trivial claim payloads.

Tests that the authorized_keys → resolver → gateway → encode_control → websockets
→ decode_control_payload pipeline preserves:

1. Unicode display names (CJK + emoji)
2. Large string values (4 KB)
3. Punctuation in values (email with +, comma+semicolon note)
4. Multiple claims (5 different keys in one authorized_keys line)
5. Empty claim values (document current behaviour)

Parser note: _split_options respects double-quoted commas, so comma-inside-quoted
values should split correctly.  _parse_options strips outer quotes.  No explicit
unicode handling beyond the UTF-8 file read — the parser treats claim values as
opaque strings, so multi-byte content should survive unchanged.
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
from tests.e2e._live_server import wait_for_condition

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _start_recording_ws_server() -> tuple[Any, int, list[dict[str, Any]]]:
    """WS server that records the first identity frame received per connection."""
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
                        return
        except Exception:
            pass

    srv = await websockets.serve(_handler, "127.0.0.1", 0)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port, frames


def _write_authorized_keys(tmp_path: Path, pubkey: asyncssh.SSHKey, options_str: str) -> Path:
    """Write a single-line authorized_keys file with the given options string."""
    body = pubkey.export_public_key().decode("ascii").strip()
    path = tmp_path / "authorized_keys"
    path.write_text(f"{options_str} {body}\n", encoding="utf-8")
    return path


async def _connect_and_capture(
    tmp_path: Path,
    options_str: str,
) -> dict[str, Any]:
    """Spin up gateway + WS server, connect with the given options, return identity frame."""
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    ak_path = _write_authorized_keys(tmp_path, client_key, options_str)
    resolver = AuthorizedKeysFileResolver(ak_path)

    ws_srv, ws_port, frames = await _start_recording_ws_server()
    gw = SshWsGateway(f"ws://127.0.0.1:{ws_port}", key_resolver=resolver)
    ssh_srv = await gw.start("127.0.0.1", 0)
    ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

    try:
        async with (
            asyncssh.connect(
                "127.0.0.1",
                port=ssh_port,
                known_hosts=None,
                username="testuser",
                client_keys=[client_key],
                preferred_auth="publickey",
                agent_path=None,
                config=[],
            ) as conn,
            conn.create_process() as proc,
        ):
            proc.stdin.write_eof()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.stdout.read(4096), timeout=2.0)
            # Deterministically wait for the gateway to emit the identity
            # frame instead of a fixed sleep.
            with contextlib.suppress(TimeoutError):
                await wait_for_condition(
                    lambda: any(f.get("type") == "identity" for f in frames),
                    timeout=3.0,
                    description="identity frame on upstream WS",
                )
    finally:
        ssh_srv.close()
        await ssh_srv.wait_closed()
        ws_srv.close()

    assert frames, "upstream WS received no identity frame"
    identity_frames = [f for f in frames if f.get("type") == "identity"]
    assert identity_frames, f"no identity frame in {frames!r}"
    return identity_frames[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIdentityClaimsVariants:
    """Verify claim payloads survive the full SSH → WS wire pipeline."""

    async def test_unicode_display_name(self, tmp_path: Path) -> None:
        """Unicode display name with CJK characters and emoji round-trips byte-identical.

        The options parser has no explicit unicode handling beyond reading the
        file as UTF-8; multi-byte code-points should pass through as opaque
        string data.
        """
        display_name = "Alice \u4e1c\u5317\u864e \U0001f42f"  # "Alice 东北虎 🐯"
        options_str = f'subject="sre:alice",claim-display_name="{display_name}"'
        frame = await _connect_and_capture(tmp_path, options_str)

        assert frame["subject"] == "sre:alice"
        assert frame["claims"]["display_name"] == display_name, (
            f"display_name did not round-trip: got {frame['claims'].get('display_name')!r}"
        )

    async def test_long_value_4kb(self, tmp_path: Path) -> None:
        """A 4 000-character note value survives the full pipeline intact."""
        # Build a deterministic lorem-ipsum-style string that is easy to verify.
        word = "Lorem ipsum dolor sit amet consectetur adipiscing elit "
        notes = (word * (4000 // len(word) + 1))[:4000]
        options_str = f'subject="sre:bob",claim-notes="{notes}"'
        frame = await _connect_and_capture(tmp_path, options_str)

        assert frame["subject"] == "sre:bob"
        received = frame["claims"].get("notes", "")
        assert received == notes, f"notes value truncated or corrupted: length {len(received)} vs {len(notes)}"

    async def test_punctuation_in_value(self, tmp_path: Path) -> None:
        """Email (with +) and note (with commas and semicolons) survive the parser.

        The _split_options function splits on commas outside quotes, so
        'alice+tag@example.com' and 'a, b; c: d' inside quoted values must
        not be split into extra tokens.
        """
        email = "alice+tag@example.com"
        note = "a, b; c: d"
        options_str = f'subject="sre:carol",claim-email="{email}",claim-note="{note}"'
        frame = await _connect_and_capture(tmp_path, options_str)

        assert frame["subject"] == "sre:carol"
        claims = frame["claims"]
        assert claims.get("email") == email, f"email claim wrong: {claims.get('email')!r} (expected {email!r})"
        assert claims.get("note") == note, f"note claim wrong: {claims.get('note')!r} (expected {note!r})"

    async def test_multiple_claims_five_keys(self, tmp_path: Path) -> None:
        """Five distinct claim keys on one authorized_keys line all arrive in the frame."""
        options_str = (
            'subject="sre:dan",'
            'claim-role="oncall",'
            'claim-display_name="Dan Operator",'
            'claim-team="platform",'
            'claim-pager="dan@pagerduty.example",'
            'claim-timezone="America/New_York"'
        )
        frame = await _connect_and_capture(tmp_path, options_str)

        assert frame["subject"] == "sre:dan"
        claims = frame["claims"]
        assert claims.get("role") == "oncall"
        assert claims.get("display_name") == "Dan Operator"
        assert claims.get("team") == "platform"
        assert claims.get("pager") == "dan@pagerduty.example"
        assert claims.get("timezone") == "America/New_York"

    async def test_empty_claim_value(self, tmp_path: Path) -> None:
        """An empty-string claim value is documented: either preserved as '' or dropped.

        The current parser does: value.strip().strip('"') on the token
        'claim-notes=""', which gives an empty string.  That empty string is
        not falsy-filtered anywhere in _parse_authorized_keys_line, so the
        expected behaviour is that it is preserved as an empty string in claims.

        If this test fails it means the parser drops empty values — that is
        the real finding to report, not a test bug.
        """
        options_str = 'subject="sre:eve",claim-notes=""'
        frame = await _connect_and_capture(tmp_path, options_str)

        assert frame["subject"] == "sre:eve"
        claims = frame["claims"]
        # Document current behaviour: empty string preserved.
        # If the parser drops it, "notes" will be absent from claims.
        assert "notes" in claims, (
            'FINDING: parser drops empty-string claim values (claim-notes="" disappears from claims). '
            f"Actual claims: {claims!r}"
        )
        assert claims["notes"] == "", (
            f"FINDING: empty claim value was not preserved as empty string, got {claims['notes']!r}"
        )

#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the capture-owner hook client (``pam_owner_hook.py``).

Decision 1 of the capture-session-ownership ARD: when ``capture_owner`` is
``hook``, uterm asks a configured URL who owns a login and uses the answer.
These tests cover the three layers: SSH-key fingerprinting, answer
validation, and the signed HTTP call itself (with an injectable transport so
no real HTTP runs in the mutation suite).
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any

import pytest

from provide.uterm.server.pam_owner_hook import (
    HookError,
    call_capture_owner_hook,
    fingerprint_ssh_keys,
    validate_hook_answer,
    validate_owner,
    validate_visibility,
)
from provide.uterm.server.webhook_signing import build_webhook_signature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ed25519_blob() -> bytes:
    """A real ssh-ed25519 public-key blob (type string + 32-byte key)."""
    key_type = b"ssh-ed25519"
    fake_key = b"\x01" * 32
    return struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", len(fake_key)) + fake_key


def _rsa_blob() -> bytes:
    """A real ssh-rsa public-key blob (type string + fake e + n)."""
    key_type = b"ssh-rsa"
    e = b"\x01\x00\x01"  # 65537
    n = b"\x00" * 128  # fake modulus
    return struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", len(e)) + e + struct.pack(">I", len(n)) + n


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ed25519_line() -> str:
    return f"publickey ssh-ed25519 {_b64(_ed25519_blob())}"


def _rsa_line() -> str:
    return f"publickey ssh-rsa {_b64(_rsa_blob())}"


def _expected_fingerprint(blob: bytes) -> str:
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _make_config(**kwargs: Any):
    """Build a PamConfig-like namespace for the hook call tests."""
    from types import SimpleNamespace

    defaults = {
        "capture_owner": "hook",
        "capture_owner_hook_url": "http://127.0.0.1:8787/hook",
        "capture_owner_hook_secret": "a" * 32,
        "capture_owner_hook_timeout_s": 1.0,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


async def _fake_post_factory(
    *,
    status: int = 200,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    raise_exc: Exception | None = None,
):
    """Build an injectable http_post that returns a fixed response or raises."""

    async def _post(url, hdrs, bdy, timeout_s):
        if raise_exc is not None:
            raise raise_exc
        return status, headers or {}, body or b""

    return _post


async def _signed_response(
    secret: str, nonce: str, answer: dict[str, Any], timestamp: str
) -> tuple[int, dict[str, str], bytes]:
    """Build a signed 200 response the hook should accept."""
    body = json.dumps(answer, separators=(",", ":")).encode("utf-8")
    sig = build_webhook_signature(secret, body, timestamp)
    return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": timestamp}, body


# ---------------------------------------------------------------------------
# fingerprint_ssh_keys
# ---------------------------------------------------------------------------


class TestFingerprintSshKeys:
    def test_none_input_returns_empty(self) -> None:
        assert fingerprint_ssh_keys(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert fingerprint_ssh_keys([]) == []

    def test_a_valid_ed25519_key_is_fingerprinted(self) -> None:
        keys = fingerprint_ssh_keys([_ed25519_line()])
        assert len(keys) == 1
        assert keys[0].key_type == "ssh-ed25519"
        assert keys[0].fingerprint == _expected_fingerprint(_ed25519_blob())

    def test_a_valid_rsa_key_is_fingerprinted(self) -> None:
        keys = fingerprint_ssh_keys([_rsa_line()])
        assert len(keys) == 1
        assert keys[0].key_type == "ssh-rsa"
        assert keys[0].fingerprint == _expected_fingerprint(_rsa_blob())

    def test_multiple_keys_are_each_fingerprinted(self) -> None:
        keys = fingerprint_ssh_keys([_ed25519_line(), _rsa_line()])
        assert len(keys) == 2
        assert keys[0].fingerprint == _expected_fingerprint(_ed25519_blob())
        assert keys[1].fingerprint == _expected_fingerprint(_rsa_blob())

    def test_more_than_four_keys_are_truncated(self) -> None:
        lines = [_ed25519_line()] * 5
        keys = fingerprint_ssh_keys(lines)
        assert len(keys) == 4

    def test_a_line_that_does_not_match_is_dropped(self) -> None:
        keys = fingerprint_ssh_keys(["not a publickey line"])
        assert keys == []

    def test_a_blob_with_a_mismatched_key_type_is_dropped(self) -> None:
        # Line says ssh-rsa but the blob says ssh-ed25519
        blob = _ed25519_blob()
        line = f"publickey ssh-rsa {_b64(blob)}"
        keys = fingerprint_ssh_keys([line])
        assert keys == []

    def test_a_truncated_blob_is_dropped(self) -> None:
        line = f"publickey ssh-ed25519 {_b64(b'too short')}"
        keys = fingerprint_ssh_keys([line])
        assert keys == []

    def test_invalid_base64_is_dropped(self) -> None:
        line = "publickey ssh-ed25519 !!!not-base64!!!"
        keys = fingerprint_ssh_keys([line])
        assert keys == []

    def test_a_valid_key_among_garbage_is_returned(self) -> None:
        keys = fingerprint_ssh_keys(["garbage", _ed25519_line(), "more garbage"])
        assert len(keys) == 1
        assert keys[0].key_type == "ssh-ed25519"


# ---------------------------------------------------------------------------
# validate_owner
# ---------------------------------------------------------------------------


class TestValidateOwner:
    def test_none_is_a_deliberate_no_owner_answer(self) -> None:
        assert validate_owner(None) is None

    def test_a_simple_username_is_accepted(self) -> None:
        assert validate_owner("alice") == "alice"

    def test_a_username_with_dots_dashes_and_at_is_accepted(self) -> None:
        assert validate_owner("alice.admin@host-01") == "alice.admin@host-01"

    def test_a_share_prefix_owner_is_refused(self) -> None:
        with pytest.raises(HookError, match="owner_share_prefix"):
            validate_owner("share:abc123")

    def test_an_owner_starting_with_a_non_alnum_is_refused(self) -> None:
        with pytest.raises(HookError, match="owner_pattern"):
            validate_owner("-alice")

    def test_an_owner_over_128_chars_is_refused(self) -> None:
        with pytest.raises(HookError, match="owner_pattern"):
            validate_owner("a" * 129)

    def test_an_owner_with_spaces_is_refused(self) -> None:
        with pytest.raises(HookError, match="owner_pattern"):
            validate_owner("alice bob")


# ---------------------------------------------------------------------------
# validate_visibility
# ---------------------------------------------------------------------------


class TestValidateVisibility:
    def test_none_keeps_operator_default(self) -> None:
        assert validate_visibility(None) is None

    def test_private_is_accepted(self) -> None:
        assert validate_visibility("private") == "private"

    def test_operator_is_accepted(self) -> None:
        assert validate_visibility("operator") == "operator"

    def test_public_is_refused(self) -> None:
        with pytest.raises(HookError, match="visibility_public"):
            validate_visibility("public")

    def test_an_unknown_value_is_refused(self) -> None:
        with pytest.raises(HookError, match="visibility_invalid"):
            validate_visibility("bogus")


# ---------------------------------------------------------------------------
# validate_hook_answer
# ---------------------------------------------------------------------------


class TestValidateHookAnswer:
    def test_a_valid_answer_with_an_owner_is_returned(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": "alice", "visibility": "private"}).encode()
        answer = validate_hook_answer(body, "n1")
        assert answer.owner == "alice"
        assert answer.visibility == "private"

    def test_a_null_owner_is_a_deliberate_no_owner(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": None}).encode()
        answer = validate_hook_answer(body, "n1")
        assert answer.owner is None

    def test_absent_visibility_defaults_to_none(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": "alice"}).encode()
        answer = validate_hook_answer(body, "n1")
        assert answer.visibility is None

    def test_unparseable_json_raises(self) -> None:
        with pytest.raises(HookError, match="body_unparseable"):
            validate_hook_answer(b"{not json", "n1")

    def test_a_non_object_body_raises(self) -> None:
        with pytest.raises(HookError, match="body_not_object"):
            validate_hook_answer(b"[1, 2, 3]", "n1")

    def test_a_nonce_mismatch_raises(self) -> None:
        body = json.dumps({"nonce": "wrong", "owner": "alice"}).encode()
        with pytest.raises(HookError, match="nonce_mismatch"):
            validate_hook_answer(body, "n1")

    def test_a_missing_nonce_raises(self) -> None:
        body = json.dumps({"owner": "alice"}).encode()
        with pytest.raises(HookError, match="nonce_mismatch"):
            validate_hook_answer(body, "n1")

    def test_a_non_string_owner_raises(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": 42}).encode()
        with pytest.raises(HookError, match="owner_not_string"):
            validate_hook_answer(body, "n1")

    def test_a_share_owner_in_the_body_raises(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": "share:abc"}).encode()
        with pytest.raises(HookError, match="owner_share_prefix"):
            validate_hook_answer(body, "n1")

    def test_a_public_visibility_in_the_body_raises(self) -> None:
        body = json.dumps({"nonce": "n1", "owner": "alice", "visibility": "public"}).encode()
        with pytest.raises(HookError, match="visibility_public"):
            validate_hook_answer(body, "n1")


# ---------------------------------------------------------------------------
# call_capture_owner_hook
# ---------------------------------------------------------------------------


class TestCallCaptureOwnerHook:
    @pytest.mark.asyncio
    async def test_not_configured_raises(self) -> None:
        cfg = _make_config(capture_owner="hook", capture_owner_hook_url=None, capture_owner_hook_secret=None)
        with pytest.raises(HookError, match="not_configured"):
            await call_capture_owner_hook(
                cfg, session_id="s1", username="alice", tty="/dev/pts/0", pid=1234, start_ticks=100, ssh_keys=[]
            )

    @pytest.mark.asyncio
    async def test_a_successful_call_returns_the_answer(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret
        nonce_captured: list[str] = []

        async def http_post(url, headers, body, timeout_s):
            # Extract the nonce from the request body
            req = json.loads(body)
            nonce_captured.append(req["nonce"])
            # Build a signed response
            ts = "1700000000"
            resp_body = json.dumps(
                {"nonce": req["nonce"], "owner": "bob", "visibility": "private"},
                separators=(",", ":"),
            ).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        answer = await call_capture_owner_hook(
            cfg,
            session_id="s1",
            username="alice",
            tty="/dev/pts/0",
            pid=1234,
            start_ticks=100,
            ssh_keys=[],
            http_post=http_post,
            clock=lambda: 1700000000.0,
        )
        assert answer.owner == "bob"
        assert answer.visibility == "private"
        assert len(nonce_captured) == 1

    @pytest.mark.asyncio
    async def test_a_null_owner_answer_is_returned(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret

        async def http_post(url, headers, body, timeout_s):
            req = json.loads(body)
            ts = "1700000000"
            resp_body = json.dumps({"nonce": req["nonce"], "owner": None}).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        answer = await call_capture_owner_hook(
            cfg,
            session_id="s1",
            username="alice",
            tty="/dev/pts/0",
            pid=1234,
            start_ticks=100,
            ssh_keys=[],
            http_post=http_post,
            clock=lambda: 1700000000.0,
        )
        assert answer.owner is None

    @pytest.mark.asyncio
    async def test_a_timeout_raises_hook_error(self) -> None:
        cfg = _make_config()

        async def http_post(url, headers, body, timeout_s):
            raise TimeoutError("timed out")

        with pytest.raises(HookError, match="timeout"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
            )

    @pytest.mark.asyncio
    async def test_a_connection_refused_raises_hook_error(self) -> None:
        cfg = _make_config()

        async def http_post(url, headers, body, timeout_s):
            raise OSError("connection refused")

        with pytest.raises(HookError, match="connection_refused"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
            )

    @pytest.mark.asyncio
    async def test_a_non_200_status_raises_hook_error(self) -> None:
        cfg = _make_config()

        async def http_post(url, headers, body, timeout_s):
            return 500, {}, b""

        with pytest.raises(HookError, match="status_500"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
            )

    @pytest.mark.asyncio
    async def test_an_unsigned_response_raises_hook_error(self) -> None:
        cfg = _make_config()

        async def http_post(url, headers, body, timeout_s):
            req = json.loads(body)
            resp_body = json.dumps({"nonce": req["nonce"], "owner": "bob"}).encode("utf-8")
            return 200, {}, resp_body

        with pytest.raises(HookError, match="signature_invalid"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
            )

    @pytest.mark.asyncio
    async def test_a_stale_timestamp_raises_hook_error(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret

        async def http_post(url, headers, body, timeout_s):
            req = json.loads(body)
            ts = "1000000000"  # very old
            resp_body = json.dumps({"nonce": req["nonce"], "owner": "bob"}).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        with pytest.raises(HookError, match="signature_invalid"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
            )

    @pytest.mark.asyncio
    async def test_a_nonce_mismatch_in_the_response_raises(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret

        async def http_post(url, headers, body, timeout_s):
            ts = "1700000000"
            resp_body = json.dumps({"nonce": "wrong", "owner": "bob"}).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        with pytest.raises(HookError, match="nonce_mismatch"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
                clock=lambda: 1700000000.0,
            )

    @pytest.mark.asyncio
    async def test_a_share_owner_in_the_response_raises(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret

        async def http_post(url, headers, body, timeout_s):
            req = json.loads(body)
            ts = "1700000000"
            resp_body = json.dumps(
                {"nonce": req["nonce"], "owner": "share:abc"},
            ).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        with pytest.raises(HookError, match="owner_share_prefix"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
                http_post=http_post,
                clock=lambda: 1700000000.0,
            )

    @pytest.mark.asyncio
    async def test_ssh_keys_are_sent_as_fingerprints(self) -> None:
        cfg = _make_config()
        secret = cfg.capture_owner_hook_secret
        sent_body: list[bytes] = []

        async def http_post(url, headers, body, timeout_s):
            sent_body.append(body)
            req = json.loads(body)
            ts = "1700000000"
            resp_body = json.dumps({"nonce": req["nonce"], "owner": "bob"}).encode("utf-8")
            sig = build_webhook_signature(secret, resp_body, ts)
            return 200, {"X-Uterm-Signature": sig, "X-Uterm-Timestamp": ts}, resp_body

        keys = fingerprint_ssh_keys([_ed25519_line(), _rsa_line()])
        await call_capture_owner_hook(
            cfg,
            session_id="s1",
            username="alice",
            tty="/dev/pts/0",
            pid=1234,
            start_ticks=100,
            ssh_keys=keys,
            http_post=http_post,
            clock=lambda: 1700000000.0,
        )
        req = json.loads(sent_body[0])
        assert len(req["ssh_keys"]) == 2
        assert req["ssh_keys"][0]["key_type"] == "ssh-ed25519"
        assert req["ssh_keys"][0]["fingerprint"] == _expected_fingerprint(_ed25519_blob())
        assert req["ssh_keys"][1]["key_type"] == "ssh-rsa"
        # The raw key never leaves the server
        assert _b64(_ed25519_blob()) not in sent_body[0].decode("utf-8")

    @pytest.mark.asyncio
    async def test_egress_blocked_raises_hook_error(self) -> None:
        cfg = _make_config(capture_owner_hook_url="http://169.254.169.254/hook")

        with pytest.raises(HookError, match="egress_blocked"):
            await call_capture_owner_hook(
                cfg,
                session_id="s1",
                username="alice",
                tty="/dev/pts/0",
                pid=1234,
                start_ticks=100,
                ssh_keys=[],
            )

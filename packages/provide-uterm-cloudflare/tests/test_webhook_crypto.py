#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the webhook-secret at-rest encryption wiring.

The AES-GCM primitives (``_aesgcm_encrypt`` / ``_aesgcm_decrypt``) are CF Web
Crypto and ``# pragma: no cover`` (exercised by real_cf integration tests);
these tests cover the envelope encode/decode, key lookup, and fallback paths
by stubbing those two primitives.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from provide.uterm.cloudflare.do import _webhook_crypto as wc

_KEY = "key"  # pragma: allowlist secret — test-only AES key placeholder


def test_webhook_key_b64_present_is_stripped() -> None:
    env = SimpleNamespace(WEBHOOK_SECRET_KEY="  k3y==  ")  # pragma: allowlist secret
    assert wc.webhook_key_b64(env) == "k3y=="


@pytest.mark.parametrize(
    "env", [SimpleNamespace(), SimpleNamespace(WEBHOOK_SECRET_KEY=""), SimpleNamespace(WEBHOOK_SECRET_KEY=None)]
)
def test_webhook_key_b64_absent_is_none(env: SimpleNamespace) -> None:
    assert wc.webhook_key_b64(env) is None


def test_is_encrypted() -> None:
    assert wc.is_encrypted("enc:v1:aa:bb") is True
    assert wc.is_encrypted("plain-secret") is False


async def test_encrypt_without_key_returns_plaintext() -> None:
    assert await wc.encrypt_secret(SimpleNamespace(), "s3cret") == "s3cret"


async def test_encrypt_with_key_builds_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_enc(_key: str, _pt: bytes) -> tuple[bytes, bytes]:
        return (b"iviviviviviv", b"CIPHER")

    monkeypatch.setattr(wc, "_aesgcm_encrypt", fake_enc)
    out = await wc.encrypt_secret(SimpleNamespace(WEBHOOK_SECRET_KEY=_KEY), "s3cret")
    assert out == f"enc:v1:{base64.b64encode(b'iviviviviviv').decode()}:{base64.b64encode(b'CIPHER').decode()}"


async def test_decrypt_legacy_plaintext_unchanged() -> None:
    assert await wc.decrypt_secret(SimpleNamespace(WEBHOOK_SECRET_KEY=_KEY), "plain") == "plain"


async def test_decrypt_envelope_without_key_returns_none() -> None:
    assert await wc.decrypt_secret(SimpleNamespace(), "enc:v1:aa:bb") is None


async def test_decrypt_malformed_envelope_returns_none() -> None:
    # Only two colons → not a 4-part envelope.
    assert await wc.decrypt_secret(SimpleNamespace(WEBHOOK_SECRET_KEY=_KEY), "enc:v1:onlythree") is None


async def test_decrypt_corrupt_ciphertext_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: object) -> bytes:
        raise ValueError("bad tag")

    monkeypatch.setattr(wc, "_aesgcm_decrypt", boom)
    iv = base64.b64encode(b"iviviviviviv").decode()
    ct = base64.b64encode(b"CIPHER").decode()
    assert await wc.decrypt_secret(SimpleNamespace(WEBHOOK_SECRET_KEY=_KEY), f"enc:v1:{iv}:{ct}") is None


async def test_decrypt_with_key_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dec(_key: str, iv: bytes, ct: bytes) -> bytes:
        assert iv == b"iviviviviviv"
        assert ct == b"CIPHER"
        return b"s3cret"

    monkeypatch.setattr(wc, "_aesgcm_decrypt", fake_dec)
    iv = base64.b64encode(b"iviviviviviv").decode()
    ct = base64.b64encode(b"CIPHER").decode()
    assert await wc.decrypt_secret(SimpleNamespace(WEBHOOK_SECRET_KEY=_KEY), f"enc:v1:{iv}:{ct}") == "s3cret"

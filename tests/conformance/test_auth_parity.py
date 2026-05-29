#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Auth parity: the FastAPI and Cloudflare auth paths are independent
implementations that drifted (the CF dev/none admin bypass, CB-3). These
tests pin their behaviour to be identical."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import jwt
import pytest

from .backends import HS256_SECRET

if TYPE_CHECKING:
    from .backends import AuthOutcome, ConformanceBackend

pytestmark = pytest.mark.asyncio

_ISS = "provide-uterm"
_AUD = "provide-uterm-server"


def _token(**overrides: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "sub": "alice",
        "roles": ["operator"],
        "iss": _ISS,
        "aud": _AUD,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    payload.update(overrides)
    return jwt.encode(payload, HS256_SECRET, algorithm="HS256")


async def _decode(backend: ConformanceBackend, token: str, *, key: str = HS256_SECRET) -> AuthOutcome:
    return await backend.decode(token, key=key, algorithms=("HS256",), issuer=_ISS, audience=_AUD)


async def test_valid_token_yields_same_subject_and_roles(backend: ConformanceBackend) -> None:
    out = await _decode(backend, _token())
    assert out.ok is True
    assert out.subject_id == "alice"
    assert "operator" in out.roles


async def test_expired_token_rejected(backend: ConformanceBackend) -> None:
    # Well past any clock-skew leeway both backends apply (~30s).
    now = int(time.time())
    expired = _token(exp=now - 3600, iat=now - 7200, nbf=now - 7200)
    out = await _decode(backend, expired)
    assert out.ok is False


async def test_wrong_key_rejected(backend: ConformanceBackend) -> None:
    out = await _decode(backend, _token(), key="a-different-32-byte-minimum-secret-key")
    assert out.ok is False


@pytest.mark.parametrize("mode", ["dev", "none"])
async def test_dev_none_auth_modes_rejected(backend: ConformanceBackend, mode: str) -> None:
    """Neither backend may accept a dev/none auth config (CB-3 removed the CF bypass)."""
    assert backend.auth_config_rejects(mode=mode) is True


async def test_algorithm_confusion_config_rejected(backend: ConformanceBackend) -> None:
    """Both backends reject HMAC (HS*) mixed with an asymmetric public key."""
    assert backend.auth_config_rejects(algorithms=("RS256", "HS256"), with_public_key=True) is True

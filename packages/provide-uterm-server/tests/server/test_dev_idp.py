#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the dev_token stub-IdP auth mode."""

from __future__ import annotations

import jwt
import pytest

from provide.uterm.server.app.auth import _validate_auth_config
from provide.uterm.server.dev_idp import (
    DEV_TOKEN_TTL_S,
    read_dev_token,
    setup_dev_idp,
)
from provide.uterm.server.models import AuthConfig, ServerBindConfig, ServerConfig


def test_setup_dev_idp_returns_jwt_string(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    token = setup_dev_idp(auth, token_path=tmp_path / "tok")
    # JWT structure: three base64url segments separated by dots.
    assert token.count(".") == 2
    assert len(token) > 60


def test_setup_dev_idp_mutates_auth_to_jwt_mode(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    setup_dev_idp(auth, token_path=tmp_path / "tok")
    assert auth.mode == "jwt"
    assert auth.jwt_algorithms == ["HS256"]
    assert auth.jwt_public_key_pem is not None
    assert len(auth.jwt_public_key_pem) >= 32  # passes the entropy floor
    assert auth.jwt_issuer
    assert auth.jwt_audience
    assert auth.worker_bearer_token is not None
    assert len(auth.worker_bearer_token) >= 32


def test_setup_dev_idp_writes_token_file_with_secure_perms(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    target = tmp_path / "subdir" / "dev_token"
    token = setup_dev_idp(auth, token_path=target)
    assert target.is_file()
    assert target.read_text() == token
    # On POSIX, mode 0600 = rw for owner only. On Windows, chmod is a noop.
    import os

    if os.name == "posix":
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


def test_setup_dev_idp_token_is_valid_jwt(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    token = setup_dev_idp(auth, token_path=tmp_path / "tok")
    decoded = jwt.decode(
        token,
        auth.jwt_public_key_pem,
        algorithms=["HS256"],
        audience=auth.jwt_audience,
        issuer=auth.jwt_issuer,
    )
    assert decoded["sub"] == "dev-user"
    assert "admin" in decoded[auth.jwt_roles_claim]
    assert decoded["exp"] > decoded["iat"]


def test_setup_dev_idp_custom_subject_and_roles(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    token = setup_dev_idp(
        auth,
        token_path=tmp_path / "tok",
        subject="alice",
        roles=("viewer", "operator"),
    )
    decoded = jwt.decode(
        token,
        auth.jwt_public_key_pem,
        algorithms=["HS256"],
        audience=auth.jwt_audience,
        issuer=auth.jwt_issuer,
    )
    assert decoded["sub"] == "alice"
    assert set(decoded[auth.jwt_roles_claim]) == {"viewer", "operator"}


def test_setup_dev_idp_each_call_uses_fresh_secret(tmp_path) -> None:
    a1 = AuthConfig(mode="dev_token")
    a2 = AuthConfig(mode="dev_token")
    setup_dev_idp(a1, token_path=tmp_path / "t1")
    setup_dev_idp(a2, token_path=tmp_path / "t2")
    assert a1.jwt_public_key_pem != a2.jwt_public_key_pem


def test_read_dev_token_round_trip(tmp_path) -> None:
    auth = AuthConfig(mode="dev_token")
    token = setup_dev_idp(auth, token_path=tmp_path / "tok")
    assert read_dev_token(tmp_path / "tok") == token


def test_read_dev_token_missing_returns_none(tmp_path) -> None:
    assert read_dev_token(tmp_path / "absent") is None


def test_validate_auth_config_dev_token_on_loopback(tmp_path, monkeypatch) -> None:
    """dev_token mode passes validation on loopback; runs full setup."""
    # Steer the file write away from ~/.cache so the test doesn't pollute.
    monkeypatch.setattr(
        "provide.uterm.server.dev_idp.DEFAULT_DEV_TOKEN_PATH",
        tmp_path / "dev_token",
    )
    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        server=ServerBindConfig(host="127.0.0.1"),
    )
    _validate_auth_config(config)
    # Mode flipped to jwt; the file got written.
    assert config.auth.mode == "jwt"
    assert (tmp_path / "dev_token").is_file()


def test_validate_auth_config_dev_token_blocked_on_non_loopback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "provide.uterm.server.dev_idp.DEFAULT_DEV_TOKEN_PATH",
        tmp_path / "dev_token",
    )
    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        server=ServerBindConfig(host="0.0.0.0"),
    )
    with pytest.raises(RuntimeError, match="only permitted when server.host is a loopback"):
        _validate_auth_config(config)


def test_dev_token_ttl_default_is_24h() -> None:
    assert DEV_TOKEN_TTL_S == 24 * 3600

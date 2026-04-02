# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from provide.terminal.pty.pam import PamError, PamSession


def test_pam_error_is_runtime_error() -> None:
    err = PamError("bad credentials")
    assert isinstance(err, RuntimeError)
    assert str(err) == "bad credentials"


def test_authenticate_before_acct_mgmt_raises() -> None:
    session = PamSession()
    with pytest.raises(PamError, match="authenticate\\(\\) must be called first"):
        session.acct_mgmt()


def test_authenticate_before_open_session_raises() -> None:
    session = PamSession()
    with pytest.raises(PamError, match="authenticate\\(\\) must be called first"):
        session.open_session()


def test_close_session_before_auth_is_noop() -> None:
    session = PamSession()
    session.close_session()  # must not raise


def test_get_env_returns_dict() -> None:
    session = PamSession()
    env = session.get_env()
    assert isinstance(env, dict)


def test_get_env_returns_copy() -> None:
    session = PamSession()
    env1 = session.get_env()
    env1["injected"] = "value"
    assert "injected" not in session.get_env()


def test_invalid_service_name_rejected() -> None:
    with pytest.raises(ValueError, match="invalid character"):
        PamSession(service="../../etc/passwd")


def test_context_manager_calls_close_on_exit() -> None:
    session = PamSession()
    session._username = "fakeuser"  # type: ignore[assignment]
    session._session_open = True
    closed: list[bool] = []

    def mock_close() -> None:
        closed.append(True)

    session.close_session = mock_close  # type: ignore[method-assign]
    with session:
        pass
    assert closed == [True]


@pytest.mark.requires_pam
def test_bad_credentials_raises_pam_error() -> None:
    """Requires /etc/pam.d/provide-terminal and a 'testuser' OS account."""
    session = PamSession()
    with pytest.raises(PamError):
        session.authenticate("testuser", "definitely_wrong_password_xyzzy")


@pytest.mark.requires_pam
def test_good_credentials_succeed() -> None:
    """Requires /etc/pam.d/provide-terminal and testuser:testpass123."""
    session = PamSession()
    session.authenticate("testuser", "testpass123")  # must not raise


def test_authenticate_validates_username() -> None:
    session = PamSession()
    with pytest.raises(ValueError, match="null byte"):
        session.authenticate("ali\x00ce", "password")


def test_authenticate_validates_password_no_null_byte() -> None:
    session = PamSession()
    with pytest.raises(ValueError, match="null byte"):
        session.authenticate("alice", "pass\x00word")

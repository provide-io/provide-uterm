# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from unittest.mock import patch

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


@pytest.mark.requires_pam_auth
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


@pytest.mark.requires_pam
def test_full_pam_lifecycle() -> None:
    """authenticate → acct_mgmt → open_session → get_env → close_session."""
    session = PamSession()
    session.authenticate("testuser", "testpass123")
    session.acct_mgmt()
    session.open_session()
    env = session.get_env()
    assert isinstance(env, dict)
    session.close_session()
    # idempotent — must not raise
    session.close_session()


@pytest.mark.requires_pam
def test_context_manager_closes_session() -> None:
    """Context manager __exit__ calls close_session."""
    session = PamSession()
    session.authenticate("testuser", "testpass123")
    session.open_session()
    with session as env:
        assert isinstance(env, dict)
        assert session._session_open  # type: ignore[attr-defined]
    assert not session._session_open  # type: ignore[attr-defined]


@pytest.mark.requires_pam
def test_open_session_get_env_copy_is_isolated() -> None:
    """get_env() returns a copy — mutations don't affect the session."""
    session = PamSession()
    session.authenticate("testuser", "testpass123")
    session.open_session()
    env = session.get_env()
    assert isinstance(env, dict)
    env["injected"] = "val"
    assert "injected" not in session.get_env()
    session.close_session()


# ── _libpam unavailable paths (patchable — no requires_pam marker) ────────────


def test_load_libpam_returns_none_when_library_not_found() -> None:
    """_load_libpam() returns None when find_library can't locate libpam."""
    import ctypes.util

    from provide.terminal.pty.pam import _load_libpam

    with patch.object(ctypes.util, "find_library", return_value=None):
        assert _load_libpam() is None


def test_load_libpam_returns_none_on_oserror() -> None:
    """_load_libpam() swallows OSError from ctypes.CDLL and returns None."""
    import ctypes
    import ctypes.util

    from provide.terminal.pty.pam import _load_libpam

    with patch.object(ctypes.util, "find_library", return_value="libpam.so.0"):
        with patch.object(ctypes, "CDLL", side_effect=OSError("not found")):
            assert _load_libpam() is None


def test_authenticate_raises_when_libpam_unavailable() -> None:
    """authenticate() raises PamError immediately when _libpam is None."""
    import provide.terminal.pty.pam as pam_mod

    with patch.object(pam_mod, "_libpam", None):
        session = PamSession()
        session._username = None  # type: ignore[assignment]
        with pytest.raises(PamError, match="libpam not available"):
            session.authenticate("alice", "secret")


def test_strerror_returns_str_when_libpam_unavailable() -> None:
    """_strerror falls back to str(retval) when _libpam is None."""
    import provide.terminal.pty.pam as pam_mod

    with patch.object(pam_mod, "_libpam", None):
        session = PamSession()
        assert session._strerror(7) == "7"  # type: ignore[attr-defined]


def test_acct_mgmt_returns_early_when_no_libpam() -> None:
    """acct_mgmt() skips the PAM call when _libpam is None."""
    import provide.terminal.pty.pam as pam_mod

    with patch.object(pam_mod, "_libpam", None):
        session = PamSession()
        session._username = "alice"  # type: ignore[assignment]
        session.acct_mgmt()  # must not raise


def test_open_session_sets_flag_when_no_libpam() -> None:
    """open_session() sets _session_open=True even when _libpam is None."""
    import provide.terminal.pty.pam as pam_mod

    with patch.object(pam_mod, "_libpam", None):
        session = PamSession()
        session._username = "alice"  # type: ignore[assignment]
        session.open_session()
        assert session._session_open  # type: ignore[attr-defined]


# ── error-path tests (skip when libpam not installed) ─────────────────────────


def _requires_libpam() -> None:
    """Skip test if libpam is not available on this system."""
    import provide.terminal.pty.pam as pam_mod

    if pam_mod._libpam is None:  # type: ignore[attr-defined]
        pytest.skip("libpam not available on this system")


def test_strerror_returns_str_when_pam_strerror_returns_none() -> None:
    """`_strerror` falls back to str(retval) when pam_strerror returns None."""
    _requires_libpam()
    import provide.terminal.pty.pam as pam_mod

    session = PamSession()
    with patch.object(pam_mod, "_pam_strerror", return_value=None):
        assert session._strerror(5) == "5"  # type: ignore[attr-defined]


def test_authenticate_raises_on_pam_start_failure() -> None:
    """authenticate() raises PamError when pam_start returns non-success."""
    _requires_libpam()
    import provide.terminal.pty.pam as pam_mod

    with patch.object(pam_mod, "_pam_start", return_value=7):
        session = PamSession()
        with pytest.raises(PamError, match="pam_start failed"):
            session.authenticate("alice", "secret")


def test_authenticate_raises_on_pam_authenticate_failure() -> None:
    """authenticate() raises PamError when pam_authenticate returns non-success."""
    _requires_libpam()
    import ctypes

    import provide.terminal.pty.pam as pam_mod

    with (
        patch.object(pam_mod, "_pam_start", return_value=0),
        patch.object(pam_mod, "_pam_authenticate", return_value=7),
        patch.object(pam_mod, "_pam_strerror", return_value=b"Authentication failure"),
        patch.object(pam_mod, "_pam_end", return_value=0),
    ):
        session = PamSession()
        # Provide a real handle so _pam_end doesn't crash
        session._handle = ctypes.c_void_p(1)  # type: ignore[attr-defined]
        with pytest.raises(PamError, match="authentication failed"):
            session.authenticate("alice", "secret")


def test_acct_mgmt_raises_on_failure() -> None:
    """acct_mgmt() raises PamError when pam_acct_mgmt returns non-success."""
    _requires_libpam()
    import ctypes

    import provide.terminal.pty.pam as pam_mod

    with (
        patch.object(pam_mod, "_pam_acct_mgmt", return_value=6),
        patch.object(pam_mod, "_pam_strerror", return_value=b"Account expired"),
    ):
        session = PamSession()
        session._username = "alice"  # type: ignore[assignment]
        session._handle = ctypes.c_void_p(1)  # type: ignore[attr-defined]
        with pytest.raises(PamError, match="pam_acct_mgmt failed"):
            session.acct_mgmt()


def test_open_session_raises_on_failure() -> None:
    """open_session() raises PamError when pam_open_session returns non-success."""
    _requires_libpam()
    import ctypes

    import provide.terminal.pty.pam as pam_mod

    with (
        patch.object(pam_mod, "_pam_open_session", return_value=6),
        patch.object(pam_mod, "_pam_strerror", return_value=b"Session error"),
    ):
        session = PamSession()
        session._username = "alice"  # type: ignore[assignment]
        session._handle = ctypes.c_void_p(1)  # type: ignore[attr-defined]
        with pytest.raises(PamError, match="pam_open_session failed"):
            session.open_session()

#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from pathlib import Path
from unittest.mock import patch

import pytest

from provide.terminal.pty.pam import PamError, PamSession

_PAM_SVC = Path("/etc/pam.d/provide-terminal")
_IN_DOCKER = Path("/.dockerenv").exists()

_skip_needs_pam = pytest.mark.skipif(not _PAM_SVC.exists(), reason="requires /etc/pam.d/provide-terminal")
_skip_needs_pam_auth = pytest.mark.skipif(
    not _PAM_SVC.exists() or _IN_DOCKER,
    reason="requires /etc/pam.d/provide-terminal with working unix_chkpwd (skipped in Docker)",
)


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
@_skip_needs_pam_auth
def test_bad_credentials_raises_pam_error() -> None:
    """Requires /etc/pam.d/provide-terminal and a 'testuser' OS account."""
    session = PamSession()
    with pytest.raises(PamError):
        session.authenticate("testuser", "definitely_wrong_password_xyzzy")


@pytest.mark.requires_pam
@_skip_needs_pam
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
@_skip_needs_pam
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
@_skip_needs_pam
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
@_skip_needs_pam
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

    with (
        patch.object(ctypes.util, "find_library", return_value="libpam.so.0"),
        patch.object(ctypes, "CDLL", side_effect=OSError("not found")),
    ):
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


def test_open_session_null_pam_envlist() -> None:
    """open_session() is a no-op on env when pam_getenvlist returns NULL."""
    _requires_libpam()
    import ctypes

    import provide.terminal.pty.pam as pam_mod

    session = PamSession()
    session._username = "alice"  # type: ignore[assignment]
    session._handle = ctypes.c_void_p(1)  # type: ignore[attr-defined]

    with (
        patch.object(pam_mod, "_pam_open_session", return_value=0),
        patch.object(pam_mod, "_pam_getenvlist", return_value=None),
    ):
        session.open_session()
    assert session.get_env() == {}


def test_open_session_populates_env_from_pam_envlist() -> None:
    """open_session() parses key=value entries from pam_getenvlist."""
    _requires_libpam()
    import ctypes

    import provide.terminal.pty.pam as pam_mod

    session = PamSession()
    session._username = "alice"  # type: ignore[assignment]
    session._handle = ctypes.c_void_p(1)  # type: ignore[attr-defined]

    # Null-terminated c_char_p array: ["MYVAR=hello", None]
    env_arr = (ctypes.c_char_p * 2)(b"MYVAR=hello", None)

    with (
        patch.object(pam_mod, "_pam_open_session", return_value=0),
        patch.object(pam_mod, "_pam_getenvlist", return_value=env_arr),
    ):
        session.open_session()
    assert session.get_env() == {"MYVAR": "hello"}


def test_close_session_skips_pam_calls_with_null_handle() -> None:
    """close_session() skips pam_close_session/pam_end when _handle is null."""
    _requires_libpam()
    session = PamSession()
    session._username = "alice"  # type: ignore[assignment]  # bypass early return
    session._session_open = True  # type: ignore[attr-defined]  # bypass early return
    # _handle is already ctypes.c_void_p(None) which is falsy — PAM calls are skipped
    session.close_session()
    assert not session._session_open  # type: ignore[attr-defined]


def test_conv_callback_echo_on_and_text_info_branches() -> None:
    """_conv handles PAM_PROMPT_ECHO_ON (username) and PAM_TEXT_INFO (no response)."""
    _requires_libpam()
    import ctypes

    from provide.terminal.pty.pam import (
        _PAM_SUCCESS,
        _make_conv_callback,
        _PamMessage,
        _PamResponse,
    )

    pam_prompt_echo_on = 2
    pam_text_info = 4

    _, cb = _make_conv_callback("alice", "secret")

    # Two messages: style=2 (returns username) and style=4 (no response)
    msgs = (_PamMessage * 2)()
    msgs[0].msg_style = pam_prompt_echo_on
    msgs[0].msg = b"Username:"
    msgs[1].msg_style = pam_text_info
    msgs[1].msg = b"Info"

    msg_ptrs = (ctypes.POINTER(_PamMessage) * 2)(
        ctypes.pointer(msgs[0]),
        ctypes.pointer(msgs[1]),
    )
    resp_ptr = ctypes.pointer(_PamResponse())
    resp_ptr_p = ctypes.pointer(resp_ptr)

    result = cb(
        2,
        ctypes.cast(msg_ptrs, ctypes.POINTER(ctypes.POINTER(_PamMessage))),
        ctypes.cast(resp_ptr_p, ctypes.POINTER(ctypes.POINTER(_PamResponse))),
        None,
    )
    assert result == _PAM_SUCCESS


def test_conv_callback_calloc_failure_returns_conv_err() -> None:
    """_conv returns _PAM_CONV_ERR when libc calloc returns 0 (allocation failure)."""
    _requires_libpam()
    import ctypes
    from unittest.mock import MagicMock

    import provide.terminal.pty.pam as pam_mod
    from provide.terminal.pty.pam import (
        _PAM_CONV_ERR,
        _make_conv_callback,
        _PamMessage,
        _PamResponse,
    )

    _, cb = _make_conv_callback("alice", "secret")

    msgs = (_PamMessage * 1)()
    msgs[0].msg_style = 1  # PAM_PROMPT_ECHO_OFF
    msgs[0].msg = b"Password:"

    msg_ptrs = (ctypes.POINTER(_PamMessage) * 1)(ctypes.pointer(msgs[0]))
    resp_ptr = ctypes.pointer(_PamResponse())
    resp_ptr_p = ctypes.pointer(resp_ptr)

    fake_libc = MagicMock()
    fake_libc.calloc.return_value = 0

    with patch.object(pam_mod, "_libc", fake_libc):
        result = cb(
            1,
            ctypes.cast(msg_ptrs, ctypes.POINTER(ctypes.POINTER(_PamMessage))),
            ctypes.cast(resp_ptr_p, ctypes.POINTER(ctypes.POINTER(_PamResponse))),
            None,
        )
    assert result == _PAM_CONV_ERR

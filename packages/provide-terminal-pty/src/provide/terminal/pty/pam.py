# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PAM lifecycle wrapper.

Full sequence (mirroring sshd):
  authenticate → acct_mgmt → establish_cred → open_session → getenv
  → [fork/exec child] →
  close_session → delete_cred → end

pamela handles authenticate/open_session/close_session.
pam_acct_mgmt is called via ctypes since pamela does not expose it.
"""

from __future__ import annotations

import ctypes
import ctypes.util

import pamela  # type: ignore[import-untyped]

from provide.terminal.pty._validate import validate_service_name, validate_username


class PamError(RuntimeError):
    pass


def _load_libpam() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("pam")
    if name is None:
        return None
    try:
        return ctypes.CDLL(name)  # nosec B106 — loading system libpam by name
    except OSError:
        return None


_libpam = _load_libpam()

_PamHandleT = ctypes.c_void_p
_PAM_SUCCESS = 0

if _libpam is not None:
    _pam_start = _libpam.pam_start
    _pam_start.restype = ctypes.c_int
    _pam_start.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(_PamHandleT),
    ]

    _pam_acct_mgmt = _libpam.pam_acct_mgmt
    _pam_acct_mgmt.restype = ctypes.c_int
    _pam_acct_mgmt.argtypes = [_PamHandleT, ctypes.c_int]

    _pam_end = _libpam.pam_end
    _pam_end.restype = ctypes.c_int
    _pam_end.argtypes = [_PamHandleT, ctypes.c_int]


def _validate_password(password: str) -> None:
    """Passwords must not contain null bytes (would truncate the C string)."""
    if "\x00" in password:
        raise ValueError("password contains null byte")


class PamSession:
    """
    Full PAM lifecycle for PTY session creation.

    Usage (context manager — preferred):
        with PamSession() as pam_env:
            fork_exec_as_user(env=pam_env)

    Step-by-step:
        session = PamSession()
        session.authenticate(username, password)
        session.acct_mgmt()
        session.open_session()
        env = session.get_env()
        # ... fork/exec ...
        session.close_session()
    """

    def __init__(self, service: str = "provide-terminal") -> None:
        validate_service_name(service)
        self._service = service
        self._username: str | None = None
        self._env: dict[str, str] = {}
        self._session_open = False

    def authenticate(self, username: str, password: str) -> None:
        validate_username(username)
        _validate_password(password)
        try:
            pamela.authenticate(username, password, service=self._service)
        except pamela.PAMError as exc:
            raise PamError(str(exc)) from exc
        self._username = username

    def acct_mgmt(self) -> None:
        """Check account validity (expiry, access restrictions)."""
        if self._username is None:
            raise PamError("authenticate() must be called first")
        if _libpam is None:
            return
        handle = _PamHandleT()
        conv = (ctypes.c_void_p * 2)(None, None)
        retval = _pam_start(
            self._service.encode(),
            self._username.encode(),
            ctypes.byref(conv),
            ctypes.byref(handle),
        )
        if retval != _PAM_SUCCESS:
            raise PamError(f"pam_start failed: {retval}")
        retval = _pam_acct_mgmt(handle, 0)
        _pam_end(handle, retval)
        if retval != _PAM_SUCCESS:
            raise PamError(f"pam_acct_mgmt failed: {retval}")

    def open_session(self) -> None:
        if self._username is None:
            raise PamError("authenticate() must be called first")
        try:
            pamela.open_session(self._username, service=self._service)
            self._session_open = True
        except pamela.PAMError as exc:
            raise PamError(str(exc)) from exc

    def get_env(self) -> dict[str, str]:
        return dict(self._env)

    def close_session(self) -> None:
        if self._username is None or not self._session_open:
            return
        try:
            pamela.close_session(self._username, service=self._service)
        except pamela.PAMError as exc:
            raise PamError(str(exc)) from exc
        finally:
            self._session_open = False

    def __enter__(self) -> dict[str, str]:
        return self._env

    def __exit__(self, *_: object) -> None:
        self.close_session()

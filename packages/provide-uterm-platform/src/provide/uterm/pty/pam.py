#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""
PAM lifecycle wrapper — direct libpam ctypes, no pamela dependency.

Full sequence (mirroring sshd):
  authenticate → acct_mgmt → open_session → get_env
  → [fork/exec child] →
  close_session → end

A single pam_handle_t is kept alive across the full lifecycle.
The conversation callback is stored on self to prevent GC before pam_end
(GC'd callbacks cause a segfault — this was pamela's Python 3.12 bug).

Response memory (resp array + each resp.resp string) is allocated via
libc calloc/strdup so that libpam can safely call free() on them.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any

from provide.uterm.pty._validate import validate_service_name, validate_username

# ── PAM return codes ──────────────────────────────────────────────────────────

_PAM_SUCCESS = 0
_PAM_CONV_ERR = 19

# msg_style values
_PAM_PROMPT_ECHO_OFF = 1  # password prompt (no echo)
_PAM_PROMPT_ECHO_ON = 2  # username prompt (with echo)
# 3 = PAM_ERROR_MSG, 4 = PAM_TEXT_INFO — no response needed

# ── ctypes structures ─────────────────────────────────────────────────────────


class _PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class _PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


_PamConvFunc = ctypes.CFUNCTYPE(
    ctypes.c_int,  # return: int
    ctypes.c_int,  # num_msg
    ctypes.POINTER(ctypes.POINTER(_PamMessage)),  # const struct pam_message **msg
    ctypes.POINTER(ctypes.POINTER(_PamResponse)),  # struct pam_response **resp
    ctypes.c_void_p,  # void *appdata_ptr
)


class _PamConv(ctypes.Structure):
    _fields_ = [("conv", _PamConvFunc), ("appdata_ptr", ctypes.c_void_p)]


# ── libc (for calloc / strdup — libpam will free() these) ────────────────────

_libc = ctypes.CDLL(None)  # libc is always available via the default handle
_libc.calloc.restype = ctypes.c_void_p
_libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
_libc.strdup.restype = ctypes.c_void_p
_libc.strdup.argtypes = [ctypes.c_char_p]

# ── libpam ────────────────────────────────────────────────────────────────────


def _load_libpam() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("pam")
    if name is None:
        return None
    try:
        # RTLD_GLOBAL: makes libpam symbols visible to PAM modules (e.g. pam_uterm.so)
        # loaded dynamically by libpam at runtime. Matches sshd's behaviour.
        return ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)  # nosec B106
    except OSError:
        return None


_libpam = _load_libpam()

if _libpam is not None:  # pragma: no branch
    _pam_start = _libpam.pam_start
    _pam_start.restype = ctypes.c_int
    _pam_start.argtypes = [
        ctypes.c_char_p,  # service
        ctypes.c_char_p,  # user
        ctypes.POINTER(_PamConv),  # conv
        ctypes.POINTER(ctypes.c_void_p),  # pamh (out)
    ]

    _pam_authenticate = _libpam.pam_authenticate
    _pam_authenticate.restype = ctypes.c_int
    _pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_acct_mgmt = _libpam.pam_acct_mgmt
    _pam_acct_mgmt.restype = ctypes.c_int
    _pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_open_session = _libpam.pam_open_session
    _pam_open_session.restype = ctypes.c_int
    _pam_open_session.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_close_session = _libpam.pam_close_session
    _pam_close_session.restype = ctypes.c_int
    _pam_close_session.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_end = _libpam.pam_end
    _pam_end.restype = ctypes.c_int
    _pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_strerror = _libpam.pam_strerror
    _pam_strerror.restype = ctypes.c_char_p
    _pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]

    _pam_getenvlist = _libpam.pam_getenvlist
    _pam_getenvlist.restype = ctypes.POINTER(ctypes.c_char_p)
    _pam_getenvlist.argtypes = [ctypes.c_void_p]


# ── public API ────────────────────────────────────────────────────────────────


class PamError(RuntimeError):
    pass


def _validate_password(password: str) -> None:
    if "\x00" in password:
        raise ValueError("password contains null byte")


def _make_conv_callback(username: str, password: str) -> tuple[_PamConv, Any]:
    """
    Build a PAM conversation struct + callback.

    IMPORTANT: the returned CFUNCTYPE object must be kept alive (stored on self)
    for as long as the pam_handle_t is open.  If Python GCs it before pam_end,
    libpam calls a dangling function pointer → segfault.  This was the root
    cause of pamela 1.2.0's segfault on Python 3.12+.

    Returns (PamConv struct, CFUNCTYPE wrapper) — caller must retain both.
    """

    def _conv(
        num_msg: int,
        msg_arr: Any,
        resp_arr_ptr: Any,
        _appdata: Any,
    ) -> int:
        # calloc: zeroed + allocated via libc so libpam can safely free() it
        ptr = _libc.calloc(num_msg, ctypes.sizeof(_PamResponse))
        if not ptr:
            return _PAM_CONV_ERR
        resp = ctypes.cast(ptr, ctypes.POINTER(_PamResponse))
        for i in range(num_msg):
            style = msg_arr[i].contents.msg_style
            if style == _PAM_PROMPT_ECHO_OFF:
                text: str | None = password
            elif style == _PAM_PROMPT_ECHO_ON:
                text = username
            else:
                text = None  # PAM_ERROR_MSG / PAM_TEXT_INFO — no response
            if text is not None:
                # strdup: allocated via libc so libpam can safely free() each resp
                resp[i].resp = ctypes.cast(_libc.strdup(text.encode()), ctypes.c_char_p)
        resp_arr_ptr[0] = resp
        return _PAM_SUCCESS

    cb = _PamConvFunc(_conv)
    conv = _PamConv()
    conv.conv = cb
    conv.appdata_ptr = None
    return conv, cb


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

    def __init__(self, service: str = "provide-uterm") -> None:
        validate_service_name(service)
        self._service = service
        self._username: str | None = None
        self._env: dict[str, str] = {}
        self._session_open = False
        self._handle: ctypes.c_void_p = ctypes.c_void_p(None)
        self._cb: Any = None  # CFUNCTYPE wrapper kept alive to prevent dangling ptr

    def _strerror(self, retval: int) -> str:
        if _libpam is None:
            return str(retval)
        msg = _pam_strerror(self._handle, retval)
        return msg.decode() if msg else str(retval)

    def authenticate(self, username: str, password: str) -> None:
        validate_username(username)
        _validate_password(password)
        if _libpam is None:
            raise PamError("libpam not available on this system")

        conv, cb = _make_conv_callback(username, password)
        handle = ctypes.c_void_p()
        retval = _pam_start(
            self._service.encode(),
            username.encode(),
            ctypes.byref(conv),
            ctypes.byref(handle),
        )
        if retval != _PAM_SUCCESS:
            raise PamError(f"pam_start failed: {retval}")

        # Store before pam_authenticate so strerror can use the handle
        self._handle = handle
        self._cb = cb  # prevent GC of the conversation callback

        retval = _pam_authenticate(handle, 0)
        if retval != _PAM_SUCCESS:
            err = self._strerror(retval)
            _pam_end(handle, retval)
            self._handle = ctypes.c_void_p(None)
            self._cb = None
            raise PamError(f"authentication failed: {err}")

        self._username = username

    def acct_mgmt(self) -> None:
        """Check account validity (expiry, access restrictions)."""
        if self._username is None:
            raise PamError("authenticate() must be called first")
        if _libpam is None or not self._handle:
            return
        retval = _pam_acct_mgmt(self._handle, 0)
        if retval != _PAM_SUCCESS:
            err = self._strerror(retval)
            raise PamError(f"pam_acct_mgmt failed: {err}")

    def open_session(self) -> None:
        if self._username is None:
            raise PamError("authenticate() must be called first")
        if _libpam is None or not self._handle:
            self._session_open = True
            return
        retval = _pam_open_session(self._handle, 0)
        if retval != _PAM_SUCCESS:
            err = self._strerror(retval)
            raise PamError(f"pam_open_session failed: {err}")
        self._session_open = True
        # Collect PAM environment (e.g. LD_PRELOAD from pam_uterm.so capture mode)
        env_list = _pam_getenvlist(self._handle)
        if env_list:
            i = 0
            while env_list[i]:
                key, _, val = env_list[i].decode().partition("=")
                self._env[key] = val
                i += 1

    def get_env(self) -> dict[str, str]:
        return dict(self._env)

    def close_session(self) -> None:
        if self._username is None or not self._session_open:
            return
        try:
            if _libpam is not None and self._handle:
                _pam_close_session(self._handle, 0)
        finally:
            self._session_open = False
            if _libpam is not None and self._handle:
                _pam_end(self._handle, _PAM_SUCCESS)
            self._handle = ctypes.c_void_p(None)
            self._cb = None  # now safe to release the callback

    def __enter__(self) -> dict[str, str]:
        return self._env

    def __exit__(self, *_: object) -> None:
        self.close_session()

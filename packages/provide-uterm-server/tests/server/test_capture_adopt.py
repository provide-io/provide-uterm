#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the startup login-record scan (Decision 3: adoption).

``pam_uterm`` writes a login record (``<cap_dir>/uterm-cap-<pid>.login``) before
notifying, so uterm can adopt logins that opened while it was down. At startup,
``scan_login_records`` lists those records, validates each one, and routes the
ones that pass through ``_on_open`` with ``adopted=True``.

Each record is trusted no further than a PAM event: lstat (regular file, uid 0,
not group/world-writable, <=16 KiB), parse (event=open, mode=capture),
capture_socket path and confinement, start_ticks against /proc, and id not
already in the registry.

These tests run in a tmp dir, faking /proc start ticks and (for the uid 0
check) the lstat result, so no real /proc or root-owned files are needed.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from provide.uterm.server.capture_adopt import (
    _LOGIN_RECORD_RE,
    _capture_dir,
    _expected_capture_socket,
    scan_login_records,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A login record's JSON line (the same bytes pam_uterm writes before notifying).
# The pid is 4242; the start_ticks is 1234567890.
_RECORD_PID = 4242
_RECORD_TICKS = 1234567890
_RECORD_USERNAME = "alice"
_RECORD_TTY = "/dev/pts/3"
_RECORD_CAP_DIR_NAME = "cap-dir"


def _login_record_json(
    pid: int = _RECORD_PID,
    username: str = _RECORD_USERNAME,
    tty: str = _RECORD_TTY,
    start_ticks: int | None = _RECORD_TICKS,
    capture_socket: str | None = "DEFAULT",
    ssh_auth_info: list[str] | None = None,
    mode: str = "capture",
    event: str = "open",
    cap_dir: str = f"/tmp/{_RECORD_CAP_DIR_NAME}",
) -> bytes:
    """Build the JSON line pam_uterm would write, newline-terminated.

    ``capture_socket`` defaults to ``<cap_dir>/uterm-cap-<pid>.sock`` (what
    pam_uterm writes). Pass ``None`` to omit the field (for a malformed record).
    """
    d: dict[str, object] = {
        "event": event,
        "username": username,
        "tty": tty,
        "pid": pid,
        "mode": mode,
    }
    if capture_socket == "DEFAULT":
        capture_socket = f"{cap_dir}/uterm-cap-{pid}.sock"
    if capture_socket is not None:
        d["capture_socket"] = capture_socket
    if start_ticks is not None:
        d["start_ticks"] = start_ticks
    if ssh_auth_info is not None:
        d["ssh_auth_info"] = ssh_auth_info
    return (json.dumps(d) + "\n").encode("utf-8")


def _write_record(
    cap_dir: Path,
    pid: int = _RECORD_PID,
    content: bytes | None = None,
    mode: int = 0o640,
) -> Path:
    """Write a login record file in cap_dir and return its path."""
    cap_dir.mkdir(parents=True, exist_ok=True)
    path = cap_dir / f"uterm-cap-{pid}.login"
    if content is None:
        content = _login_record_json(pid=pid, cap_dir=str(cap_dir))
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _root_stat(path: Path) -> os.stat_result:
    """A stat_result like the real one but with uid=0 (root-owned).

    Uses ``os.lstat`` directly (not ``path.lstat()``) to avoid recursing
    through the patched ``Path.lstat`` that calls this helper.
    """
    real = os.lstat(str(path))
    # stat_result takes (st_mode, st_ino, st_dev, st_nlink, st_uid, st_gid,
    # st_size, st_atime, st_mtime, st_ctime)
    return os.stat_result(
        (
            real.st_mode,
            real.st_ino,
            real.st_dev,
            real.st_nlink,
            0,  # uid=0 (root)
            real.st_gid,
            real.st_size,
            real.st_atime,
            real.st_mtime,
            real.st_ctime,
        )
    )


@contextlib.contextmanager
def _root_owned_files(cap_dir: Path):
    """Patch Path.lstat so files in cap_dir report uid=0 (root-owned)."""
    real_lstat = Path.lstat

    def patched(self: Path) -> os.stat_result:
        try:
            if self.resolve().is_relative_to(cap_dir.resolve()):
                return _root_stat(self)
        except Exception:
            pass
        return real_lstat(self)

    with patch.object(Path, "lstat", patched):
        yield


def _mock_pam_cfg(
    cap_dir: Path | None = None,
    mode: str = "capture",
    notify_socket: str | None = None,
) -> MagicMock:
    """A PamConfig mock for capture mode with cap_dir as the capture directory."""
    cfg = MagicMock()
    cfg.mode = mode
    cfg.capture_socket_dir = str(cap_dir) if cap_dir is not None else None
    cfg.notify_socket = notify_socket
    cfg.capture_owner = "pam_user"
    cfg.auto_session = False
    cfg.relay_url = None
    cfg.relay_token = None
    return cfg


def _mock_registry(session_exists: bool = False) -> AsyncMock:
    """A SessionRegistry mock whose get_definition returns a session or None."""
    registry = AsyncMock()
    if session_exists:
        registry.get_definition.return_value = MagicMock()  # truthy
    else:
        registry.get_definition.return_value = None
    return registry


# ---------------------------------------------------------------------------
# _LOGIN_RECORD_RE, _capture_dir, _expected_capture_socket
# ---------------------------------------------------------------------------


class TestLoginRecordRe:
    def test_matches_uterm_cap_pid_login(self) -> None:
        m = _LOGIN_RECORD_RE.match("uterm-cap-12345.login")
        assert m is not None
        assert m.group("pid") == "12345"

    def test_rejects_non_matching(self) -> None:
        assert _LOGIN_RECORD_RE.match("uterm-cap-12345.sock") is None
        assert _LOGIN_RECORD_RE.match("not-a-record") is None
        assert _LOGIN_RECORD_RE.match("uterm-cap-.login") is None


class TestCaptureDir:
    def test_uses_capture_socket_dir_when_set(self) -> None:
        cfg = _mock_pam_cfg(cap_dir=Path("/run/cap"))
        assert _capture_dir(cfg) == "/run/cap"

    def test_falls_back_to_notify_socket_parent(self) -> None:
        cfg = _mock_pam_cfg(cap_dir=None, notify_socket="/run/uterm-notify.sock")
        assert _capture_dir(cfg) == "/run"

    def test_returns_none_without_either(self) -> None:
        cfg = _mock_pam_cfg(cap_dir=None, notify_socket=None)
        assert _capture_dir(cfg) is None


class TestExpectedCaptureSocket:
    def test_builds_dir_uterm_cap_pid_sock(self) -> None:
        assert _expected_capture_socket("/run", 12345) == "/run/uterm-cap-12345.sock"


# ---------------------------------------------------------------------------
# scan_login_records — the scan itself
# ---------------------------------------------------------------------------


class TestScanLoginRecords:
    async def test_no_capture_dir_returns_zero_zero(self, tmp_path: Path) -> None:
        cfg = _mock_pam_cfg(cap_dir=None, notify_socket=None)
        registry = _mock_registry()
        adopted, skipped = await scan_login_records(cfg, registry)
        assert (adopted, skipped) == (0, 0)

    async def test_notify_mode_returns_zero_zero(self, tmp_path: Path) -> None:
        cfg = _mock_pam_cfg(cap_dir=tmp_path, mode="notify")
        registry = _mock_registry()
        adopted, skipped = await scan_login_records(cfg, registry)
        assert (adopted, skipped) == (0, 0)

    async def test_missing_dir_returns_zero_zero(self, tmp_path: Path) -> None:
        cfg = _mock_pam_cfg(cap_dir=tmp_path / "nonexistent")
        registry = _mock_registry()
        adopted, skipped = await scan_login_records(cfg, registry)
        assert (adopted, skipped) == (0, 0)

    async def test_adopts_a_valid_record(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 1
        assert skipped == 0
        mock_on_open.assert_awaited_once()
        # adopted=True is passed through
        call_kwargs = mock_on_open.call_args
        assert call_kwargs.kwargs.get("adopted") is True

    async def test_skips_dead_login_at_debug(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=9999999,  # different ticks → login is dead
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_when_start_ticks_none_from_proc(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=None,  # /proc unreadable
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_record_without_start_ticks(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        # Write a record with no start_ticks field
        content = _login_record_json(start_ticks=None, cap_dir=str(cap_dir))
        _write_record(cap_dir, pid=_RECORD_PID, content=content)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_non_root_owned_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        # No _root_owned_files patch → the file is owned by the test user, not root.
        with (
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_group_writable_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID, mode=0o660)  # group-writable
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_world_writable_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID, mode=0o606)  # other-writable
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_too_large_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        # Write a record with a huge payload (>16 KiB)
        big = _login_record_json() + b" " * (17 * 1024)
        _write_record(cap_dir, pid=_RECORD_PID, content=big)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_bad_json_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID, content=b"not json\n")
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_close_event_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        content = _login_record_json(event="close", cap_dir=str(cap_dir))
        _write_record(cap_dir, pid=_RECORD_PID, content=content)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_notify_mode_event_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        content = _login_record_json(mode="notify", cap_dir=str(cap_dir))
        _write_record(cap_dir, pid=_RECORD_PID, content=content)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_socket_mismatch_at_warning(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        # A record whose capture_socket doesn't match <dir>/uterm-cap-<pid>.sock
        content = _login_record_json(
            capture_socket="/run/evil.sock",
        )
        _write_record(cap_dir, pid=_RECORD_PID, content=content)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_skips_already_in_registry_at_debug(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=_RECORD_PID)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry(session_exists=True)

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 0
        assert skipped == 1
        mock_on_open.assert_not_awaited()

    async def test_ignores_non_record_files(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        cap_dir.mkdir(parents=True)
        # A socket file and a non-matching file
        (cap_dir / "uterm-cap-12345.sock").touch()
        (cap_dir / "random.txt").write_text("hello")
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        adopted, skipped = await scan_login_records(cfg, registry)
        assert (adopted, skipped) == (0, 0)

    async def test_adopts_multiple_records(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        _write_record(cap_dir, pid=1001)
        _write_record(cap_dir, pid=1002)
        _write_record(cap_dir, pid=1003)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, skipped = await scan_login_records(cfg, registry)

        assert adopted == 3
        assert skipped == 0
        assert mock_on_open.await_count == 3
        # All calls pass adopted=True
        for call in mock_on_open.await_args_list:
            assert call.kwargs.get("adopted") is True

    async def test_passes_ssh_auth_info_through(self, tmp_path: Path) -> None:
        cap_dir = tmp_path / _RECORD_CAP_DIR_NAME
        content = _login_record_json(
            ssh_auth_info=["publickey ssh-ed25519 AAAA..."],
            cap_dir=str(cap_dir),
        )
        _write_record(cap_dir, pid=_RECORD_PID, content=content)
        cfg = _mock_pam_cfg(cap_dir=cap_dir)
        registry = _mock_registry()

        with (
            _root_owned_files(cap_dir),
            patch(
                "provide.uterm.server.pam_integration._process_start_ticks",
                return_value=_RECORD_TICKS,
            ),
            patch("provide.uterm.server.pam_integration._on_open", new_callable=AsyncMock) as mock_on_open,
        ):
            adopted, _ = await scan_login_records(cfg, registry)

        assert adopted == 1
        # The event passed to _on_open carries the ssh_auth_info
        ev = mock_on_open.call_args.args[0]
        assert ev.ssh_auth_info == ["publickey ssh-ed25519 AAAA..."]

# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import pwd

import pytest

from provide.terminal.pty.uid_map import ResolvedUser, UidMap, UidMapError


def _current() -> pwd.struct_passwd:
    return pwd.getpwuid(os.getuid())


def test_default_resolves_current_user() -> None:
    pw = _current()
    result = UidMap().resolve(pw.pw_name)
    assert result.uid == pw.pw_uid
    assert result.gid == pw.pw_gid
    assert result.name == pw.pw_name
    assert result.home == pw.pw_dir
    assert result.shell == pw.pw_shell


def test_unknown_username_raises() -> None:
    with pytest.raises(UidMapError, match="no such OS user"):
        UidMap().resolve("__no_such_user_xyzzy__")


def test_run_as_uid_override() -> None:
    pw = _current()
    result = UidMap().resolve("anything", run_as_uid=pw.pw_uid)
    assert result.uid == pw.pw_uid


def test_run_as_uid_with_explicit_gid() -> None:
    pw = _current()
    result = UidMap().resolve("anything", run_as_uid=pw.pw_uid, run_as_gid=0)
    assert result.uid == pw.pw_uid
    assert result.gid == 0


def test_run_as_name_override() -> None:
    pw = _current()
    result = UidMap().resolve("anything", run_as=pw.pw_name)
    assert result.uid == pw.pw_uid
    assert result.name == pw.pw_name


def test_run_as_numeric_string() -> None:
    pw = _current()
    result = UidMap().resolve("anything", run_as=str(pw.pw_uid))
    assert result.uid == pw.pw_uid


def test_run_as_uid_colon_gid() -> None:
    pw = _current()
    spec = f"{pw.pw_uid}:{pw.pw_gid}"
    result = UidMap().resolve("anything", run_as=spec)
    assert result.uid == pw.pw_uid
    assert result.gid == pw.pw_gid


def test_run_as_unknown_name_raises() -> None:
    with pytest.raises(UidMapError, match="no such OS user"):
        UidMap().resolve("anything", run_as="__no_such_user_xyzzy__")


def test_table_entry_by_name() -> None:
    pw = _current()
    table = {"appuser": pw.pw_name}
    result = UidMap(table).resolve("appuser")
    assert result.uid == pw.pw_uid


def test_table_numeric_uid() -> None:
    pw = _current()
    table = {"appuser": str(pw.pw_uid)}
    result = UidMap(table).resolve("appuser")
    assert result.uid == pw.pw_uid


def test_table_uid_colon_gid() -> None:
    pw = _current()
    table = {"appuser": f"{pw.pw_uid}:{pw.pw_gid}"}
    result = UidMap(table).resolve("appuser")
    assert result.uid == pw.pw_uid
    assert result.gid == pw.pw_gid


def test_table_wildcard_fallback() -> None:
    pw = _current()
    table = {"*": pw.pw_name}
    result = UidMap(table).resolve("anyone")
    assert result.uid == pw.pw_uid


def test_run_as_takes_priority_over_table() -> None:
    pw = _current()
    table = {"appuser": "root"}  # would fail unless we're root
    result = UidMap(table).resolve("appuser", run_as_uid=pw.pw_uid)
    assert result.uid == pw.pw_uid


def test_resolved_user_is_dataclass() -> None:
    pw = _current()
    result = UidMap().resolve(pw.pw_name)
    assert isinstance(result, ResolvedUser)


def test_invalid_spec_format_raises() -> None:
    with pytest.raises(UidMapError, match="no such OS user"):
        UidMap().resolve("anything", run_as="__no_such__")


def test_uid_colon_gid_non_numeric_raises() -> None:
    with pytest.raises(ValueError):
        UidMap().resolve("anything", run_as="notanint:notanint")


def test_resolve_validates_username() -> None:
    """resolve() must reject usernames with null bytes before touching pwd."""
    with pytest.raises(ValueError, match="null byte"):
        UidMap().resolve("ali\x00ce")


def test_resolve_empty_username_with_run_as_uid() -> None:
    """resolve() with empty username skips username validation and uses run_as_uid."""
    pw = _current()
    result = UidMap().resolve("", run_as_uid=pw.pw_uid)
    assert result.uid == pw.pw_uid


def test_from_uid_unknown_uid_returns_synthetic_user() -> None:
    """_from_uid() returns a synthetic ResolvedUser for an unknown UID."""
    result = UidMap().resolve("", run_as_uid=999999999)
    assert result.uid == 999999999
    assert result.home == "/"
    assert result.shell == "/bin/sh"
    assert result.name == "999999999"

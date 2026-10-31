#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``[session_persistence]``: what it accepts, and the directory it names.

``state_dir`` is named outright, never derived, so two servers cannot share
one by accident, and it must be an absolute path to a 0700 directory this
server's user owns, since the files in it hold unredacted screens.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from provide.uterm.server import capture_record as cr
from provide.uterm.server.capture_state import build_capture_state
from provide.uterm.server.config_schema import UtermServerConfig

from .capture_state_helpers import mode

REQUIRED = (
    "session_persistence.state_dir is required when session_persistence.persist_capture "
    "is on or session_persistence.previous_keep_days is above 0"
)


def _config(**settings: object) -> UtermServerConfig:
    return UtermServerConfig.model_validate({"session_persistence": settings})


def test_the_settings_default_to_off_with_a_thousand_rows_and_no_directory() -> None:
    settings = UtermServerConfig().session_persistence
    assert (settings.persist_capture, settings.previous_keep_days, settings.previous_scrollback_lines) == (
        False,
        0,
        1000,
    )
    assert settings.state_dir is None


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        (
            {"previous_keep_days": -1},
            "session_persistence.previous_keep_days must be between 0 (keep no previous sessions) and 3650, got: -1",
        ),
        (
            {"previous_keep_days": 3651},
            "session_persistence.previous_keep_days must be between 0 (keep no previous sessions) and 3650, got: 3651",
        ),
        (
            {"previous_scrollback_lines": -1},
            "session_persistence.previous_scrollback_lines must be between 0 and 10000, got: -1",
        ),
        (
            {"previous_scrollback_lines": 10001},
            "session_persistence.previous_scrollback_lines must be between 0 and 10000, got: 10001",
        ),
        ({"state_dir": "state/"}, "session_persistence.state_dir must be an absolute path, got: state"),
        ({"state_dir": ""}, "session_persistence.state_dir must be an absolute path, got: ."),
        ({"persist_capture": True}, REQUIRED),
        ({"previous_keep_days": 1}, REQUIRED),
    ],
)
def test_a_setting_that_will_not_do_is_refused(settings: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        _config(**settings)


@pytest.mark.parametrize(
    "settings",
    [
        {"previous_keep_days": 3650, "previous_scrollback_lines": 10000, "state_dir": "/var/lib/u"},
        {"previous_keep_days": 0, "previous_scrollback_lines": 0},
        {"state_dir": "/var/lib/u"},
    ],
)
def test_settings_within_their_bounds_are_accepted(settings: dict[str, object]) -> None:
    _config(**settings)


def test_a_directory_is_kept_as_written_less_a_trailing_slash() -> None:
    assert _config(persist_capture=True, state_dir="/var/lib/u/").session_persistence.state_dir == Path("/var/lib/u")


def test_an_unknown_setting_is_refused() -> None:
    with pytest.raises(ValueError, match="persist_captures"):
        _config(persist_captures=True)


def test_no_store_is_built_when_both_features_are_off() -> None:
    assert build_capture_state(UtermServerConfig()) is None


@pytest.mark.parametrize(("persist", "keep_days"), [(True, 0), (False, 7), (True, 7)])
def test_the_store_is_made_in_the_directory_named(tmp_path: Path, persist: bool, keep_days: int) -> None:
    config = _config(
        persist_capture=persist, previous_keep_days=keep_days, previous_scrollback_lines=5, state_dir=tmp_path / "s"
    )
    s = build_capture_state(config)
    assert s is not None
    assert mode(tmp_path / "s") == 0o700
    assert s._live_dir == tmp_path / "s" / "live"
    assert (s.persist, s.keeps_previous, s.scrollback_lines) == (persist, keep_days > 0, 5 if keep_days else 0)


def test_an_existing_private_directory_is_used(tmp_path: Path) -> None:
    (tmp_path / "s").mkdir(mode=0o700)
    (tmp_path / "s").chmod(0o700)
    assert build_capture_state(_config(persist_capture=True, state_dir=tmp_path / "s")) is not None


@pytest.mark.parametrize(
    ("prepare", "error"),
    [
        (lambda d: (d.mkdir(), d.chmod(0o755)), ": mode 0755, not 0700"),
        (lambda d: d.write_text("x"), ": not a directory this user owns"),
        (lambda d: d.symlink_to(d.parent), ": not a directory this user owns"),
    ],
)
def test_a_directory_that_is_not_private_stops_the_server(
    tmp_path: Path, prepare: Callable[[Path], object], error: str
) -> None:
    state = tmp_path / "s"
    prepare(state)
    with pytest.raises(ValueError, match=re.escape(f"session_persistence.state_dir {state}{error}")):
        build_capture_state(_config(persist_capture=True, state_dir=state))


def test_a_directory_another_user_owns_stops_the_server(tmp_path: Path) -> None:
    (tmp_path / "s").mkdir(mode=0o700)
    (tmp_path / "s").chmod(0o700)
    with (
        patch.object(cr.os, "geteuid", return_value=os.geteuid() + 1),
        pytest.raises(ValueError, match="not a directory this user owns"),
    ):
        build_capture_state(_config(persist_capture=True, state_dir=tmp_path / "s"))


def test_a_directory_whose_parent_is_missing_stops_the_server(tmp_path: Path) -> None:
    state = tmp_path / "a" / "s"
    with pytest.raises(ValueError, match=re.escape(f"session_persistence.state_dir {state}: cannot create: ENOENT")):
        build_capture_state(_config(persist_capture=True, state_dir=state))

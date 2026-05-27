#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Default server-config mutation-killing tests split from test_config.py."""

from __future__ import annotations

from provide.uterm.server.config import default_server_config


def test_default_server_config_session_display_name() -> None:
    config = default_server_config()
    session = config.sessions[0]
    assert session.display_name == "Provide Shell", (
        f"Default session display_name must be 'Provide Shell', got {session.display_name!r}"
    )


def test_default_server_config_session_connector_type_is_shell() -> None:
    config = default_server_config()
    session = config.sessions[0]
    assert session.connector_type == "shell", (
        f"Default session connector_type must be 'shell', got {session.connector_type!r}"
    )


def test_default_server_config_session_input_mode_is_open() -> None:
    config = default_server_config()
    session = config.sessions[0]
    assert session.input_mode == "open", f"Default session input_mode must be 'open', got {session.input_mode!r}"


def test_default_server_config_session_auto_start_is_true() -> None:
    config = default_server_config()
    session = config.sessions[0]
    assert session.auto_start is True, f"Default session auto_start must be True, got {session.auto_start!r}"


def test_default_server_config_session_tags_exact() -> None:
    config = default_server_config()
    session = config.sessions[0]
    assert session.tags == ["shell", "reference"], (
        f"Default session tags must be ['shell', 'reference'], got {session.tags!r}"
    )

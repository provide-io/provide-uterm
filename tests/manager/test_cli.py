#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.manager.cli."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from provide.terminal.manager.cli import main


def test_main_parses_args_and_runs():
    mock_manager = MagicMock()
    mock_manager.run = AsyncMock()

    with (
        patch("sys.argv", ["uterm-manager", "--host", "0.0.0.0", "--port", "9999", "--log-level", "debug"]),
        patch("provide.terminal.manager.app.create_manager_app") as mock_create,
        patch("asyncio.run") as mock_run,
    ):
        mock_create.return_value = (MagicMock(), mock_manager)
        main()

    # Verify config was created with parsed args
    call_args = mock_create.call_args
    cfg = call_args[0][0]
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9999
    assert cfg.log_level == "debug"
    mock_run.assert_called_once()


def test_main_defaults():
    mock_manager = MagicMock()
    with (
        patch("sys.argv", ["uterm-manager"]),
        patch("provide.terminal.manager.app.create_manager_app") as mock_create,
        patch("asyncio.run"),
    ):
        mock_create.return_value = (MagicMock(), mock_manager)
        main()
    cfg = mock_create.call_args[0][0]
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 2272


def test_main_unknown_args():
    """Unknown args are silently ignored."""
    mock_manager = MagicMock()
    with (
        patch("sys.argv", ["uterm-manager", "--unknown", "value"]),
        patch("provide.terminal.manager.app.create_manager_app") as mock_create,
        patch("asyncio.run"),
    ):
        mock_create.return_value = (MagicMock(), mock_manager)
        main()  # should not raise

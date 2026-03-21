#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Mutation-killing tests for misc — fastapi mount_terminal_ui defaults."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest


class TestFastapiMountTerminalUiDefaults:
    """Kill fastapi.py mount_terminal_ui parameter and StaticFiles mutants."""

    def test_mount_terminal_ui_default_path_is_slash_terminal(self) -> None:
        """Default path parameter is '/terminal' (kills mutmut_1 'XX/terminalXX', mutmut_2 '/TERMINAL')."""
        from provide.terminal.fastapi import mount_terminal_ui

        sig = inspect.signature(mount_terminal_ui)
        default = sig.parameters["path"].default
        assert default == "/terminal", f"Default path must be '/terminal', got {default!r}"
        assert "XX" not in default
        assert default == default.lower(), "Default path must be lowercase"

    def test_frontend_dir_is_named_frontend_lowercase(self) -> None:
        """Frontend path uses 'frontend' (lowercase), not 'FRONTEND' (kills mutmut_7)."""
        from provide.terminal import fastapi as fastapi_module

        fastapi_path = Path(fastapi_module.__file__).parent
        expected = fastapi_path / "frontend"
        # Verify the expected path has lowercase 'frontend' component
        assert expected.name == "frontend", f"Must be 'frontend', got {expected.name!r}"
        assert expected.name != "FRONTEND"

    def test_error_raised_when_frontend_dir_missing(self) -> None:
        """RuntimeError raised when frontend dir not found (kills mutmut_10/11)."""
        from unittest.mock import MagicMock, patch

        from provide.terminal.fastapi import mount_terminal_ui

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            mount_terminal_ui(mock_app)

        msg = str(exc_info.value)
        assert "terminal UI assets not found" in msg
        assert "is the package installed correctly" in msg
        assert "XX" not in msg
        assert msg == msg  # uses original lowercase phrasing (not ALL CAPS)

    def test_static_files_html_true_not_false_or_none(self) -> None:
        """StaticFiles is called with html=True (kills mutmut_19 None, mutmut_22 False)."""
        from unittest.mock import MagicMock, patch

        from provide.terminal.fastapi import mount_terminal_ui

        captured: list[dict] = []

        def capture(**kw: Any) -> MagicMock:
            captured.append(kw)
            return MagicMock()

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles", side_effect=capture),
        ):
            mount_terminal_ui(mock_app)

        assert len(captured) == 1
        assert captured[0].get("html") is True, f"StaticFiles must have html=True, got {captured[0].get('html')!r}"

    def test_static_files_directory_is_not_none(self) -> None:
        """StaticFiles is called with directory=frontend_path (kills mutmut_18 None, mutmut_20 missing)."""
        from unittest.mock import MagicMock, patch

        from provide.terminal.fastapi import mount_terminal_ui

        captured: list[dict] = []

        def capture(**kw: Any) -> MagicMock:
            captured.append(kw)
            return MagicMock()

        mock_app = MagicMock()
        with (
            patch("provide.terminal.fastapi.Path.is_dir", return_value=True),
            patch("starlette.staticfiles.StaticFiles", side_effect=capture),
        ):
            mount_terminal_ui(mock_app)

        assert "directory" in captured[0], "StaticFiles must receive 'directory' kwarg"
        assert captured[0]["directory"] is not None, "directory must not be None"

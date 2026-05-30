#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for no-echo password keystroke masking in HostedSessionRuntime._log_send."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime


def _make_session(session_id: str = "test-session") -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Test Session",
        connector_type="shell",
        auto_start=False,
    )


def _make_runtime() -> HostedSessionRuntime:
    return HostedSessionRuntime(
        _make_session(),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
    )


def _make_mock_logger() -> AsyncMock:
    mock = AsyncMock()
    mock.log_send = AsyncMock()
    mock.log_send_masked = AsyncMock()
    mock.log_screen = AsyncMock()
    mock.log_event = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# Test 1: Masks at password prompt
# ---------------------------------------------------------------------------


class TestMasksAtPasswordPrompt:
    async def test_log_send_masked_called_not_log_send(self) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": "login as: tim\nPassword:"})
        await rt._log_send("s")

        mock_logger.log_send_masked.assert_called_once_with(1)
        mock_logger.log_send.assert_not_called()

    async def test_masked_byte_count_matches_cp437_encoding(self) -> None:
        """Byte count passed to log_send_masked reflects cp437-encoded length."""
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": "Password:"})
        # three-char string: 3 bytes in cp437
        await rt._log_send("abc")

        mock_logger.log_send_masked.assert_called_once_with(3)
        mock_logger.log_send.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Common prompts arm the flag
# ---------------------------------------------------------------------------

_COMMON_PROMPTS = [
    "Password:",
    "login as: tim\nPassword:",
    "tim@host's password:",
    "[sudo] password for tim:",
    "Enter passphrase for key '/root/.ssh/id_rsa':",
]


class TestCommonPromptsArmedFlag:
    @pytest.mark.parametrize("screen_suffix", _COMMON_PROMPTS)
    async def test_flag_set_after_snapshot(self, screen_suffix: str) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": screen_suffix})

        assert rt._at_password_prompt is True

    async def test_flag_not_set_for_normal_prompt(self) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": "tim@host:~$ "})

        assert rt._at_password_prompt is False


# ---------------------------------------------------------------------------
# Test 3: Normal prompt does NOT mask
# ---------------------------------------------------------------------------


class TestNormalPromptDoesNotMask:
    async def test_log_send_called_normally_at_shell_prompt(self) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": "tim@host:~$ "})
        await rt._log_send("ls")

        mock_logger.log_send.assert_called_once_with("ls")
        mock_logger.log_send_masked.assert_not_called()
        assert rt._at_password_prompt is False


# ---------------------------------------------------------------------------
# Test 4: Flag clears after non-password snapshot
# ---------------------------------------------------------------------------


class TestFlagClears:
    async def test_flag_cleared_after_non_password_screen(self) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        # Arm
        await rt._log_snapshot({"screen": "Password:"})
        assert rt._at_password_prompt is True

        # Clear
        await rt._log_snapshot({"screen": "tim@host:~$ "})
        assert rt._at_password_prompt is False

    async def test_send_after_clear_is_unmasked(self) -> None:
        rt = _make_runtime()
        mock_logger = _make_mock_logger()
        rt._logger = mock_logger

        await rt._log_snapshot({"screen": "Password:"})
        await rt._log_snapshot({"screen": "Last login: Fri May 30\ntim@host:~$ "})
        await rt._log_send("whoami")

        mock_logger.log_send.assert_called_once_with("whoami")
        mock_logger.log_send_masked.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: No logger → no crash
# ---------------------------------------------------------------------------


class TestNoLoggerNoCrash:
    async def test_log_send_no_logger_at_password_prompt(self) -> None:
        rt = _make_runtime()
        rt._logger = None
        # Manually arm (as if a previous snapshot set it)
        rt._at_password_prompt = True  # type: ignore[attr-defined]
        # Should not raise
        await rt._log_send("x")

    async def test_log_snapshot_no_logger_still_updates_flag(self) -> None:
        """_log_snapshot re-derives the flag even when logger is None."""
        rt = _make_runtime()
        rt._logger = None

        await rt._log_snapshot({"screen": "Password:"})
        assert rt._at_password_prompt is True

        await rt._log_snapshot({"screen": "tim@host:~$ "})
        assert rt._at_password_prompt is False

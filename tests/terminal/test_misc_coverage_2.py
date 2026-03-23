#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Simple coverage gap tests (part 2): emulator, cli, replay/raw, server/config,
server/registry, io."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 10. emulator.py line 83->98 — cached snapshot returned (not dirty)
# ---------------------------------------------------------------------------


class TestEmulatorCachedSnapshot:
    def test_get_snapshot_uses_cache_when_not_dirty(self) -> None:
        """Line 83->98: _last_snapshot is not None and not dirty → return cached."""
        from provide.terminal.emulator import TerminalEmulator

        emulator = TerminalEmulator(cols=80, rows=25)
        emulator.process(b"Hello world")
        # First call builds cache
        snap1 = emulator.get_snapshot()
        assert snap1["screen"] is not None
        # Second call: _dirty=False, _last_snapshot is set → should use cache (line 98)
        snap2 = emulator.get_snapshot()
        assert snap2["screen"] == snap1["screen"]
        assert snap2["screen_hash"] == snap1["screen_hash"]


# ---------------------------------------------------------------------------
# 11. cli.py line 72 — AttributeError raised when SSHTransport is None
# ---------------------------------------------------------------------------


class TestCliSSHTransportMissing:
    def test_ssh_transport_attribute_error_exits(self) -> None:
        """Line 72: raise AttributeError('SSHTransport') when attr is None."""
        import argparse

        from provide.terminal.cli import _cmd_proxy

        args = argparse.Namespace(
            transport="ssh",
            host="bbs.example.com",
            bbs_port=22,
            port=8080,
            bind="0.0.0.0",
            path="/ws/term",
        )

        # Mock the ssh module to have SSHTransport=None
        mock_ssh_mod = MagicMock()
        mock_ssh_mod.SSHTransport = None

        with patch("importlib.import_module", return_value=mock_ssh_mod):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_proxy(args)
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# 12. replay/raw.py line 33->26 — raw_b64 is empty → don't extend
# ---------------------------------------------------------------------------


class TestReplayRawBranch:
    def test_rebuild_raw_stream_skips_empty_raw_bytes_b64(self) -> None:
        """Line 33->26: raw_b64 is empty → out.extend not called for that record."""
        from provide.terminal.replay.raw import rebuild_raw_stream

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.jsonl"
            out_path = Path(tmpdir) / "out.bin"

            import base64

            # Record with empty raw_bytes_b64
            log_path.write_text(
                json.dumps({"event": "read", "data": {"raw_bytes_b64": ""}})
                + "\n"
                + json.dumps({"event": "read", "data": {"raw_bytes_b64": base64.b64encode(b"hello").decode()}})
                + "\n"
                + json.dumps({"event": "other", "data": {}})
                + "\n",
                encoding="utf-8",
            )

            rebuild_raw_stream(log_path, out_path)
            data = out_path.read_bytes()
            assert data == b"hello"  # only second record contributed


# ---------------------------------------------------------------------------
# 13. server/config.py line 57->59 — relative recording directory resolved
# ---------------------------------------------------------------------------


class TestServerConfigRelativeDir:
    def test_load_server_config_resolves_relative_recording_dir(self) -> None:
        """Line 57->59: if recording dir is relative, resolve against config file dir."""
        from provide.terminal.server.config import load_server_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "server.toml"
            # Write a config with a relative recording directory
            cfg_path.write_text(
                '[recording]\ndirectory = "recordings"\n',
                encoding="utf-8",
            )
            config = load_server_config(cfg_path)
            # The directory should have been resolved to an absolute path
            assert config.recording.directory.is_absolute()
            assert config.recording.directory == (cfg_path.parent / "recordings").resolve()


# ---------------------------------------------------------------------------
# 14. server/registry.py line 78 — runtime.stop() called on ephemeral delete
# ---------------------------------------------------------------------------


class TestRegistryRuntimeStop:
    async def test_ephemeral_session_runtime_stop_called(self) -> None:
        """Line 77->78: runtime is not None → await runtime.stop()."""
        from provide.terminal.server.models import RecordingConfig, SessionDefinition
        from provide.terminal.server.registry import SessionRegistry

        mock_hub = MagicMock()
        mock_hub.force_release_hijack = AsyncMock(return_value=True)
        mock_hub.get_last_snapshot = AsyncMock(return_value=None)
        mock_hub.get_recent_events = AsyncMock(return_value=[])
        mock_hub.browser_count = AsyncMock(return_value=0)
        mock_hub.on_worker_empty = None

        session = SessionDefinition(
            session_id="ephem1",
            display_name="Ephemeral",
            connector_type="shell",
            auto_start=False,
            ephemeral=True,
        )

        reg = SessionRegistry(
            [session],
            hub=mock_hub,
            public_base_url="http://localhost:9999",
            recording=RecordingConfig(),
        )

        # Install a mock runtime so we can verify stop() is called
        mock_runtime = AsyncMock()
        mock_runtime.stop = AsyncMock()
        async with reg._lock:
            reg._runtimes["ephem1"] = mock_runtime

        await reg._on_worker_empty("ephem1")

        mock_runtime.stop.assert_awaited()


# ---------------------------------------------------------------------------
# 15. io.py lines 127->129, 141->143 — on_prompt_rejected=None branches
# ---------------------------------------------------------------------------


class TestIoBranches:
    async def test_on_prompt_rejected_none_when_not_idle(self) -> None:
        """Line 127->129: on_prompt_rejected is None, not_idle branch is skipped."""
        from provide.terminal.io import PromptWaiter

        call_count = 0

        def mock_snapshot() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: not idle
                return {
                    "screen": "screen text",
                    "prompt_detected": {"is_idle": False, "prompt_id": "menu_main", "input_type": "menu"},
                }
            # Second call: idle
            return {
                "screen": "screen text",
                "prompt_detected": {"is_idle": True, "prompt_id": "menu_main", "input_type": "menu"},
            }

        mock_session = MagicMock()
        mock_session.snapshot = mock_snapshot
        mock_session.wait_for_update = AsyncMock(return_value=True)
        mock_session.seconds_until_idle = MagicMock(return_value=0.1)

        waiter = PromptWaiter(mock_session)
        result = await waiter.wait_for_prompt(
            timeout_ms=2000,
            require_idle=True,
            on_prompt_rejected=None,  # This is the branch we're testing
        )
        # Should eventually succeed after retrying
        assert result is not None

    async def test_on_prompt_rejected_none_when_callback_rejects(self) -> None:
        """Line 141->143: on_prompt_rejected is None, callback_reject branch skipped."""
        from provide.terminal.io import PromptWaiter

        call_count = 0

        def mock_snapshot() -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "screen": "screen text",
                "prompt_detected": {"is_idle": True, "prompt_id": "menu_main", "input_type": "menu"},
            }

        mock_session = MagicMock()
        mock_session.snapshot = mock_snapshot
        mock_session.wait_for_update = AsyncMock(return_value=True)

        waiter = PromptWaiter(mock_session)

        on_detected_calls = 0

        def on_detected(info: dict) -> bool:
            nonlocal on_detected_calls
            on_detected_calls += 1
            # First call rejects, second accepts
            return on_detected_calls > 1

        result = await waiter.wait_for_prompt(
            timeout_ms=2000,
            on_prompt_detected=on_detected,
            on_prompt_rejected=None,  # The branch we're testing
        )
        assert result is not None

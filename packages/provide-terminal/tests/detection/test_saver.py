#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for screen saver functionality."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from provide.terminal.detection.saver import ScreenSaver


@pytest.fixture
def temp_knowledge_dir(tmp_path: Path) -> Path:
    """Create temporary knowledge directory."""
    return tmp_path / ".bbs-knowledge"


@pytest.fixture
def screen_saver(temp_knowledge_dir: Path) -> ScreenSaver:
    """Create screen saver instance."""
    return ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=True)


@pytest.fixture
def sample_snapshot() -> dict[str, Any]:
    """Create sample screen snapshot."""
    return {
        "screen": "Test screen content\nLine 2\nLine 3",
        "screen_hash": "abc123def456",
        "captured_at": time.time(),
        "cursor": {"x": 10, "y": 5},
        "cols": 80,
        "rows": 25,
        "term": "ANSI",
    }


class TestScreenSaverInitialization:
    """Test screen saver initialization."""

    def test_initialization(self, temp_knowledge_dir: Path) -> None:
        """Test basic initialization."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=True)

        assert saver._base_dir == temp_knowledge_dir
        assert saver._namespace == "tradewars"
        assert saver._enabled is True
        assert len(saver._saved_hashes) == 0

    def test_initialization_no_namespace(self, temp_knowledge_dir: Path) -> None:
        """Test initialization without namespace."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace=None, enabled=True)

        assert saver._namespace is None
        assert saver._enabled is True

    def test_initialization_disabled(self, temp_knowledge_dir: Path) -> None:
        """Test initialization with saving disabled."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=False)

        assert saver._enabled is False


class TestScreenSaverConfiguration:
    """Test screen saver configuration methods."""

    def test_set_enabled(self, screen_saver: ScreenSaver) -> None:
        """Test enabling/disabling screen saving."""
        screen_saver.set_enabled(False)
        assert screen_saver._enabled is False

        screen_saver.set_enabled(True)
        assert screen_saver._enabled is True

    def test_set_namespace(self, screen_saver: ScreenSaver) -> None:
        """Test changing namespace."""
        screen_saver.set_namespace("other_game")
        assert screen_saver._namespace == "other_game"

        screen_saver.set_namespace(None)
        assert screen_saver._namespace is None

    def test_get_screens_dir_with_namespace(self, temp_knowledge_dir: Path) -> None:
        """Test screens directory path with namespace."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=True)

        screens_dir = saver.get_screens_dir()

        assert screens_dir == temp_knowledge_dir / "games" / "tradewars" / "screens"

    def test_get_screens_dir_without_namespace(self, temp_knowledge_dir: Path) -> None:
        """Test screens directory path without namespace."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace=None, enabled=True)

        screens_dir = saver.get_screens_dir()

        assert screens_dir == temp_knowledge_dir / "shared" / "screens"

    def test_get_saved_count(self, screen_saver: ScreenSaver) -> None:
        """Test getting count of saved screens."""
        assert screen_saver.get_saved_count() == 0

        screen_saver._saved_hashes.add("hash1")
        screen_saver._saved_hashes.add("hash2")

        assert screen_saver.get_saved_count() == 2


class TestScreenSaving:
    """Test screen saving functionality."""

    def test_save_screen_basic(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test basic screen saving."""
        result = screen_saver.save_screen(sample_snapshot)

        assert result is not None
        assert result.exists()
        assert result.parent == screen_saver.get_screens_dir()
        assert screen_saver.get_saved_count() == 1

    def test_save_screen_with_prompt_id(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test saving screen with prompt ID."""
        result = screen_saver.save_screen(sample_snapshot, prompt_id="prompt.warp")

        assert result is not None
        assert "prompt.warp" in result.name
        assert result.exists()

    def test_save_screen_disabled(self, temp_knowledge_dir: Path, sample_snapshot: dict[str, Any]) -> None:
        """Test that disabled saver doesn't save."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=False)

        result = saver.save_screen(sample_snapshot)

        assert result is None
        assert saver.get_saved_count() == 0

    def test_save_screen_no_duplicates(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test that duplicate screens aren't saved twice."""
        result1 = screen_saver.save_screen(sample_snapshot)
        result2 = screen_saver.save_screen(sample_snapshot)

        assert result1 is not None
        assert result2 is None  # Duplicate not saved
        assert screen_saver.get_saved_count() == 1

    def test_save_screen_force_duplicate(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test forcing save of duplicate screen."""
        result1 = screen_saver.save_screen(sample_snapshot)
        result2 = screen_saver.save_screen(sample_snapshot, force=True)

        assert result1 is not None
        assert result2 is not None
        assert result1 != result2  # Different files
        # Count stays at 1 because hash is same, just forced save
        assert screen_saver.get_saved_count() == 1

    def test_save_screen_missing_data(self, screen_saver: ScreenSaver) -> None:
        """Test saving with missing required data."""
        # Missing screen content
        result = screen_saver.save_screen({"screen_hash": "abc123"})
        assert result is None

        # Missing hash
        result = screen_saver.save_screen({"screen": "content"})
        assert result is None

        # Empty screen
        result = screen_saver.save_screen({"screen": "", "screen_hash": "abc123"})
        assert result is None


class TestScreenFileFormat:
    """Test screen file formatting."""

    def test_file_content_format(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test that saved file has correct format."""
        result = screen_saver.save_screen(sample_snapshot)

        assert result is not None
        content = result.read_text()

        assert "SCREEN CAPTURE" in content
        assert "Hash: abc123def456" in content
        assert "Cursor: (10, 5)" in content
        assert "Size: 80x25" in content
        assert "Terminal: ANSI" in content
        assert "Test screen content" in content

    def test_file_content_with_prompt_id(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test file content includes prompt ID."""
        result = screen_saver.save_screen(sample_snapshot, prompt_id="prompt.command")

        assert result is not None
        content = result.read_text()

        assert "Prompt ID: prompt.command" in content

    def test_file_naming_format(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test that file names follow correct format."""
        result = screen_saver.save_screen(sample_snapshot)

        assert result is not None
        # Format: YYYYMMDD-HHMMSS-hash.txt
        assert result.suffix == ".txt"
        parts = result.stem.split("-")
        assert len(parts) >= 3  # Date, time, hash (may have more dashes in hash)

    def test_file_naming_with_prompt(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test file naming includes prompt suffix."""
        result = screen_saver.save_screen(sample_snapshot, prompt_id="prompt.warp")

        assert result is not None
        assert "prompt.warp" in result.name
        assert result.suffix == ".txt"


class TestScreenSaverHashTracking:
    """Test screen hash tracking."""

    def test_clear_saved_hashes(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test clearing saved hashes."""
        screen_saver.save_screen(sample_snapshot)
        assert screen_saver.get_saved_count() == 1

        screen_saver.clear_saved_hashes()
        assert screen_saver.get_saved_count() == 0

    def test_clear_allows_resave(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """Test that clearing hashes allows re-saving same screen."""
        result1 = screen_saver.save_screen(sample_snapshot)
        assert result1 is not None

        # Try to save again (should be skipped)
        result2 = screen_saver.save_screen(sample_snapshot)
        assert result2 is None

        # Clear and save again (should work)
        screen_saver.clear_saved_hashes()
        result3 = screen_saver.save_screen(sample_snapshot)
        assert result3 is not None

    def test_multiple_unique_screens(self, screen_saver: ScreenSaver) -> None:
        """Test saving multiple unique screens."""
        snapshots = [{"screen": f"Screen {i}", "screen_hash": f"hash{i}", "captured_at": time.time()} for i in range(5)]

        for snapshot in snapshots:
            result = screen_saver.save_screen(snapshot)
            assert result is not None

        assert screen_saver.get_saved_count() == 5


class TestScreenSaverMetadata:
    """Test screen metadata in saved files."""

    def test_metadata_cursor_info(self, screen_saver: ScreenSaver) -> None:
        """Test cursor information in metadata."""
        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
            "cursor": {"x": 42, "y": 15},
        }

        result = screen_saver.save_screen(snapshot)
        assert result is not None
        content = result.read_text()

        assert "Cursor: (42, 15)" in content

    def test_metadata_cursor_default(self, screen_saver: ScreenSaver) -> None:
        """Test default cursor values when not provided."""
        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
        }

        result = screen_saver.save_screen(snapshot)
        assert result is not None
        content = result.read_text()

        assert "Cursor: (0, 0)" in content

    def test_metadata_prompt_detection_info(self, screen_saver: ScreenSaver) -> None:
        """Test prompt detection metadata."""
        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
            "prompt_detected": {
                "input_type": "single_key",
                "is_idle": True,
            },
        }

        result = screen_saver.save_screen(snapshot, prompt_id="prompt.command")
        assert result is not None
        content = result.read_text()

        assert "Input Type: single_key" in content
        assert "Idle: True" in content

    def test_metadata_timing_info(self, screen_saver: ScreenSaver) -> None:
        """Test timing metadata."""
        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
            "time_since_last_change": 2.5,
            "cursor_at_end": True,
        }

        result = screen_saver.save_screen(snapshot)
        assert result is not None
        content = result.read_text()

        assert "Time Since Last Change: 2.50s" in content
        assert "Cursor at End: True" in content


class TestScreenSaverDirectoryCreation:
    """Test directory creation."""

    def test_creates_screens_directory(self, temp_knowledge_dir: Path) -> None:
        """Test that screens directory is created."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="tradewars", enabled=True)

        # Directory shouldn't exist yet
        screens_dir = saver.get_screens_dir()
        assert not screens_dir.exists()

        # Save a screen
        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
        }
        saver.save_screen(snapshot)

        # Now directory should exist
        assert screens_dir.exists()
        assert screens_dir.is_dir()

    def test_creates_parent_directories(self, temp_knowledge_dir: Path) -> None:
        """Test that parent directories are created."""
        saver = ScreenSaver(base_dir=temp_knowledge_dir, namespace="nested/game", enabled=True)

        snapshot = {
            "screen": "content",
            "screen_hash": "hash1",
            "captured_at": time.time(),
        }
        result = saver.save_screen(snapshot)

        assert result is not None
        # Verify full path was created
        assert result.parent.exists()
        assert result.parent.parent.exists()


class TestScreenSaverErrorPaths:
    """Test error handling in ScreenSaver."""

    def test_write_failure_does_not_add_hash(
        self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If write_text raises, the hash must NOT be added to _saved_hashes."""
        from pathlib import Path

        def bad_write(content: str, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", bad_write)

        with pytest.raises(OSError, match="disk full"):
            screen_saver.save_screen(sample_snapshot)

        # Hash must not have been recorded
        assert screen_saver.get_saved_count() == 0
        assert sample_snapshot["screen_hash"] not in screen_saver._saved_hashes

    def test_write_failure_allows_retry(
        self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a write failure the same screen can be attempted again."""
        from pathlib import Path

        call_count = {"n": 0}
        original_write = Path.write_text

        def flaky_write(self_path: Path, content: str, *args: object, **kwargs: object) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("transient error")
            original_write(self_path, content, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", flaky_write)

        # First attempt fails
        with pytest.raises(OSError):
            screen_saver.save_screen(sample_snapshot)
        assert screen_saver.get_saved_count() == 0

        # Second attempt (monkeypatch restored — use raw call which now succeeds)
        result = screen_saver.save_screen(sample_snapshot)
        assert result is not None
        assert screen_saver.get_saved_count() == 1

    def test_dup_fallback_exhaustion_raises(self, screen_saver: ScreenSaver, sample_snapshot: dict[str, Any]) -> None:
        """When all 10,000 dup slots are taken, OSError is raised instead of overwriting."""
        # Save once to create the base file
        result = screen_saver.save_screen(sample_snapshot)
        assert result is not None
        base_stem = result.stem
        screens_dir = screen_saver.get_screens_dir()

        # Pre-create all 10,000 dup slots
        for i in range(1, 10_000):
            (screens_dir / f"{base_stem}-dup{i}.txt").write_text("dummy")

        with pytest.raises(OSError, match="10,000"):
            screen_saver.save_screen(sample_snapshot, force=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

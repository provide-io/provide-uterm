#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Screen saver for persisting unique screens to disk."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class ScreenSaver:
    """Saves unique screens to disk in organized directory structure."""

    def __init__(self, base_dir: Path, namespace: str | None = None, enabled: bool = True) -> None:
        """Initialize screen saver.

        Args:
            base_dir: Base directory for screen storage (e.g., .bbs-knowledge)
            namespace: Game/namespace for organizing screens
            enabled: Whether screen saving is enabled
        """
        self._base_dir = base_dir
        self._namespace = namespace
        self._enabled = enabled
        self._saved_hashes: set[str] = set()

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable screen saving."""
        self._enabled = enabled

    def set_namespace(self, namespace: str | None) -> None:
        """Set namespace for screen organization."""
        self._namespace = namespace

    def get_screens_dir(self) -> Path:
        """Get directory for saving screens.

        Returns:
            Path to screens directory
        """
        if self._namespace:
            return self._base_dir / "games" / self._namespace / "screens"
        return self._base_dir / "shared" / "screens"

    def save_screen(
        self,
        snapshot: dict[str, Any],
        prompt_id: str | None = None,
        force: bool = False,
    ) -> Path | None:
        """Save screen snapshot to disk.

        **Blocking I/O warning:** this method performs synchronous disk writes
        and directory creation.  It is called from
        ``DetectionEngine.process_screen()`` (an ``async def``), which means it
        blocks the event loop on every save.  At low save rates (a few per
        second) this is acceptable; at high rates consider offloading via
        ``asyncio.get_event_loop().run_in_executor(None, ...)``.

        Args:
            snapshot: Screen snapshot with screen, screen_hash, captured_at, etc.
            prompt_id: Optional prompt ID if detected
            force: Force save even if hash already saved

        Returns:
            Path to saved screen file, or None if not saved
        """
        if not self._enabled:
            return None

        screen = snapshot.get("screen", "")
        screen_hash = snapshot.get("screen_hash", "")
        captured_at = snapshot.get("captured_at", time.time())

        if not screen or not screen_hash:
            return None

        # Skip if already saved (unless forced)
        if not force and screen_hash in self._saved_hashes:
            return None

        # Create screens directory
        screens_dir = self.get_screens_dir()
        screens_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(captured_at))
        hash_short = screen_hash[:8]
        prompt_suffix = f"-{prompt_id}" if prompt_id else ""
        filename = f"{timestamp}-{hash_short}{prompt_suffix}.txt"

        screen_file = screens_dir / filename
        if force and screen_file.exists():
            # Make forced saves stable and non-destructive by creating a distinct
            # filename instead of overwriting the prior capture.
            stem = screen_file.stem
            candidate = screen_file
            for i in range(1, 10_000):
                candidate = screens_dir / f"{stem}-dup{i}.txt"
                if not candidate.exists():
                    screen_file = candidate
                    break
            else:
                raise OSError(f"Could not find free filename after 10,000 attempts for {filename}")

        # Write screen with metadata header
        content = self._format_screen_file(snapshot, prompt_id)
        screen_file.write_text(content)

        # Track saved hash only after confirmed write
        self._saved_hashes.add(screen_hash)

        return screen_file

    def _format_screen_file(self, snapshot: dict[str, Any], prompt_id: str | None) -> str:
        """Format screen file with metadata header.

        Args:
            snapshot: Screen snapshot
            prompt_id: Optional prompt ID

        Returns:
            Formatted file content
        """
        lines = [
            "=" * 80,
            "SCREEN CAPTURE",
            "=" * 80,
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(snapshot.get('captured_at', time.time())))}",
            f"Hash: {snapshot.get('screen_hash', 'unknown')}",
            f"Cursor: ({snapshot.get('cursor', {}).get('x', 0)}, {snapshot.get('cursor', {}).get('y', 0)})",
            f"Size: {snapshot.get('cols', 80)}x{snapshot.get('rows', 25)}",
            f"Terminal: {snapshot.get('term', 'ANSI')}",
        ]

        if prompt_id:
            lines.append(f"Prompt ID: {prompt_id}")

        if "prompt_detected" in snapshot:
            detected = snapshot["prompt_detected"]
            lines.append(f"Input Type: {detected.get('input_type', 'unknown')}")
            lines.append(f"Idle: {detected.get('is_idle', False)}")

        if snapshot.get("cursor_at_end") is not None:
            lines.append(f"Cursor at End: {snapshot['cursor_at_end']}")

        if snapshot.get("time_since_last_change") is not None:
            lines.append(f"Time Since Last Change: {snapshot['time_since_last_change']:.2f}s")

        lines.extend(
            [
                "=" * 80,
                "",
                snapshot.get("screen", ""),
            ]
        )

        return "\n".join(lines)

    def clear_saved_hashes(self) -> None:
        """Clear the set of saved screen hashes.

        Useful for forcing re-save of all screens.
        """
        self._saved_hashes.clear()

    def get_saved_count(self) -> int:
        """Get count of saved unique screens.

        Returns:
            Number of unique screen hashes saved
        """
        return len(self._saved_hashes)

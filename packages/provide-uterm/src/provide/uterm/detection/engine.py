#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from provide.uterm.detection.buffer import BufferManager
from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.extractor import extract_kv
from provide.uterm.detection.loader import load_ruleset
from provide.uterm.detection.models import PromptDetection, PromptDetectionDiagnostics, PromptMatch

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from provide.uterm.detection.buffer import ScreenBuffer
    from provide.uterm.detection.rules import RuleSet
    from provide.uterm.detection.saver import ScreenSaver

logger = logging.getLogger(__name__)


class DetectionEngine:
    """Rule-based prompt detection and data extraction engine.

    Accepts rules at init, compiles patterns, and provides both sync
    _sync_process_screen() and async process_screen() for prompt detection
    + KV extraction.  The async variant also handles buffering, idle
    detection, screen saving, and callable hooks.
    """

    def __init__(
        self,
        rules: RuleSet | Path | str,
        *,
        normalizer: Callable[[str], str] | None = None,
        buffer_size: int = 50,
        idle_threshold_s: float = 2.0,
        screen_saver: ScreenSaver | None = None,
        namespace: str | None = None,
    ) -> None:
        """Compile rules into a PromptDetector + KVExtractor.

        Args:
            rules: A RuleSet object, path to rules.json, or JSON string.
            normalizer: Optional function to normalize prompt region text
                before fingerprinting (e.g. strip volatile timer fields).
            buffer_size: Maximum number of screens to keep in the internal
                BufferManager (default 50).
            idle_threshold_s: Seconds of screen stability before is_idle is True.
            screen_saver: Optional ScreenSaver for persisting screens to disk.
            namespace: Game/namespace identifier (informational; passed to saver).

        Raises:
            ValueError: If rules cannot be loaded or parsed.
        """
        self._normalizer = normalizer
        self._enabled = True
        self._last_fingerprint: str = ""
        self._last_match: PromptMatch | None = None
        ruleset = load_ruleset(rules)
        patterns = ruleset.to_prompt_patterns()
        self._detector = PromptDetector(patterns, normalizer=normalizer)

        # Async-processing state
        self._buffer_manager = BufferManager(max_size=buffer_size)
        self._idle_threshold_s = idle_threshold_s
        self._screen_saver = screen_saver
        self._namespace = namespace
        self._hooks: list[Callable[..., Awaitable[None]]] = []

    # ------------------------------------------------------------------
    # Sync detection (pure CPU)
    # ------------------------------------------------------------------

    def _sync_process_screen(self, snapshot: dict[str, Any]) -> PromptDetection | None:
        """Detect prompt and extract KV data from a screen snapshot.

        Sync — pure CPU regex matching, no I/O.
        Returns None if no prompt matched or engine is disabled.
        """
        if not self._enabled:
            return None

        fingerprint = self._detector.prompt_fingerprint(snapshot)
        if fingerprint and fingerprint == self._last_fingerprint:
            prompt_match = self._last_match
        else:
            prompt_match = self._detector.detect_prompt(snapshot)
            self._last_fingerprint = fingerprint
            self._last_match = prompt_match

        if prompt_match is None:
            return None

        kv_data: dict[str, Any] = {}
        if prompt_match.kv_extract:
            extracted = extract_kv(snapshot.get("screen", ""), prompt_match.kv_extract)
            if extracted:
                kv_data = extracted

        return PromptDetection(
            prompt_id=prompt_match.prompt_id,
            input_type=prompt_match.input_type,
            kv_data=kv_data,
            match=prompt_match,
        )

    # ------------------------------------------------------------------
    # Async processing (buffering + saving + hooks)
    # ------------------------------------------------------------------

    async def process_screen(self, snapshot: dict[str, Any]) -> PromptDetection | None:
        """Detect prompt with buffering, idle detection, screen saving, and hooks.

        Detection is pure CPU (no executor needed — sub-millisecond regex).
        Hooks are awaited sequentially after detection.

        Args:
            snapshot: Screen snapshot dict (must have 'screen' and 'screen_hash').

        Returns:
            PromptDetection (with is_idle and buffer populated) or None.
        """
        buffer: ScreenBuffer = self._buffer_manager.add_screen(snapshot)
        is_idle: bool = self._buffer_manager.detect_idle_state(self._idle_threshold_s)

        detection = self._sync_process_screen(snapshot)

        if detection and detection.match:
            buffer.matched_prompt_id = detection.match.prompt_id

        if self._screen_saver is not None:
            try:
                prompt_id = detection.prompt_id if detection else None
                self._screen_saver.save_screen(snapshot, prompt_id=prompt_id)
            except Exception:
                logger.warning("Screen saver failed; detection continues", exc_info=True)

        if detection is not None:
            detection.is_idle = is_idle
            detection.buffer = buffer

        for hook in self._hooks:
            try:
                await hook(snapshot, detection, buffer, is_idle)
            except Exception:
                logger.warning("Hook %r raised; continuing", hook, exc_info=True)

        return detection

    # ------------------------------------------------------------------
    # Hook management
    # ------------------------------------------------------------------

    def add_hook(self, fn: Callable[..., Awaitable[None]]) -> None:
        """Register an async hook called after each process_screen() call.

        Hook signature: ``async def hook(snapshot, detection, buffer, is_idle)``
        """
        self._hooks.append(fn)

    # ------------------------------------------------------------------
    # Properties and configuration
    # ------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        """True if the screen has been stable for >= idle_threshold_s."""
        return self._buffer_manager.detect_idle_state(self._idle_threshold_s)

    @property
    def namespace(self) -> str | None:
        """Game/namespace identifier."""
        return self._namespace

    def set_namespace(self, ns: str | None) -> None:
        """Update namespace and propagate to the ScreenSaver if present."""
        self._namespace = ns
        if self._screen_saver is not None:
            self._screen_saver.set_namespace(ns)

    def get_screen_saver_status(self) -> dict[str, Any]:
        """Return screen-saver status dict."""
        if self._screen_saver is None:
            return {"enabled": False}
        return {
            "enabled": self._screen_saver._enabled,
            "screens_dir": str(self._screen_saver.get_screens_dir()),
            "saved_count": self._screen_saver.get_saved_count(),
            "namespace": self._screen_saver._namespace,
        }

    def set_screen_saving(self, enabled: bool) -> None:
        """Enable or disable the ScreenSaver."""
        if self._screen_saver is not None:
            self._screen_saver.set_enabled(enabled)

    def debug_state(self) -> dict[str, Any]:
        """Return internal debug info (avoids direct private-attr access by callers)."""
        bm = self._buffer_manager
        recent = bm.get_recent(n=1)
        return {
            "idle_threshold_s": self._idle_threshold_s,
            "namespace": self._namespace,
            "screen_buffer": {
                "size": len(bm._buffer),
                "max_size": bm._buffer.maxlen,
                "is_idle": bm.detect_idle_state() if recent else False,
                "last_change_seconds_ago": recent[0].time_since_last_change if recent else 0.0,
            },
            "screen_saver": self.get_screen_saver_status() if self._screen_saver is not None else None,
        }

    # ------------------------------------------------------------------
    # Existing API (unchanged)
    # ------------------------------------------------------------------

    def detect_with_diagnostics(self, snapshot: dict[str, Any]) -> PromptDetectionDiagnostics:
        """Detect with partial-match info for debugging."""
        return self._detector.detect_prompt_with_diagnostics(snapshot)

    def reload_rules(self, rules: RuleSet | Path | str) -> None:
        """Hot-reload rules. Transactional: on failure, old rules remain active.

        Raises:
            ValueError: If new rules cannot be loaded.
        """
        ruleset = load_ruleset(rules)
        patterns = ruleset.to_prompt_patterns()
        # Only swap if compilation succeeds
        self._detector = PromptDetector(patterns, normalizer=self._normalizer)
        self._last_fingerprint = ""
        self._last_match = None

    @property
    def detector(self) -> PromptDetector:
        """Access the underlying PromptDetector."""
        return self._detector

    @property
    def pattern_count(self) -> int:
        """Number of compiled patterns."""
        return self._detector.pattern_count

    @property
    def enabled(self) -> bool:
        """Whether the engine processes screens."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

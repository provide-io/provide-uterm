#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Pattern compile/reload machinery for :class:`PromptDetector`.

Extracted from ``detector.py`` to keep that module under the LOC budget.
These are module-level free functions that take the detector instance as
their first argument; ``PromptDetector`` keeps thin wrapper methods that
forward here, so the public method/attribute surface is unchanged.

The logger is deliberately named ``provide.uterm.detection.detector`` (the
detector module's name) rather than this module's ``__name__``: existing
diagnostics and tests observe compile log records under that logger, and
keeping the emitter name stable preserves that contract after the split.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.detection.detector import PromptDetector

# Emit under the detector module's logger name (see module docstring).
logger = logging.getLogger("provide.uterm.detection.detector")


class DetectorPatternCompileError(ValueError):
    """Raised in ``strict=True`` mode when a pattern fails to compile.

    The non-strict default merely logs failures and continues with the
    surviving patterns — useful for "soft" environments where a broken
    rule shouldn't take the whole detector offline. Production deploys
    that load curated rules should pass ``strict=True`` so a typo in a
    rules file is caught at startup instead of silently degrading
    detection.
    """


def compile_patterns(detector: PromptDetector) -> list[tuple[re.Pattern[str], dict[str, Any]]]:
    """Compile regex patterns for efficient matching.

    Returns:
        List of (compiled_regex, pattern_dict) tuples
    """
    compiled = []
    failed_patterns: list[dict[str, Any]] = []

    logger.info("pattern_compile_start count=%d", len(detector._patterns))

    for pattern in detector._patterns:
        try:
            regex = re.compile(pattern["regex"], re.MULTILINE)
            compiled.append((regex, pattern))
            logger.debug("pattern_compile_ok pattern_id=%s", pattern.get("id", "unknown"))
        except re.error as e:
            # Pattern compilation failed - emit diagnostic.
            # Reaching ``re.error`` means ``pattern["regex"]`` was successfully
            # read above (only ``re.compile`` failed), so the ``regex`` key is
            # always present here — no ``.get`` default is reachable.
            regex_value = pattern["regex"]
            failed_patterns.append(
                {
                    "id": pattern.get("id", "unknown"),
                    "regex": regex_value,
                    "error": str(e),
                }
            )
            logger.exception(
                "pattern_compile_failed pattern_id=%s regex=%s error=%s",
                pattern.get("id", "unknown"),
                regex_value,
                str(e),
            )
            continue
        except KeyError as e:
            # Pattern missing required 'regex' key
            logger.exception(
                "pattern_compile_invalid_structure pattern_id=%s missing_key=%s",
                pattern.get("id", "unknown"),
                str(e),
            )
            failed_patterns.append(
                {
                    "id": pattern.get("id", "unknown"),
                    "error": f"Missing key: {e}",
                }
            )
            continue

    logger.info("pattern_compile_complete succeeded=%d failed=%d", len(compiled), len(failed_patterns))

    if failed_patterns:
        # Every entry appended above carries both ``id`` and ``error`` keys
        # (set on both the ``re.error`` and ``KeyError`` branches), so direct
        # indexing is correct and leaves no dead ``.get`` default to mutate.
        logger.error(
            "pattern_compile_failures count=%d failed=%s",
            len(failed_patterns),
            [{"id": p["id"], "error": p["error"]} for p in failed_patterns],
        )
        detector._compile_failures = failed_patterns
        if detector._strict:
            summary = ", ".join(f"{p['id']}: {p['error']}" for p in failed_patterns)
            raise DetectorPatternCompileError(
                f"{len(failed_patterns)} pattern(s) failed to compile in strict mode: {summary}"
            )

    return compiled


def swap_patterns(detector: PromptDetector, candidate: list[dict[str, Any]]) -> None:
    """Atomically replace ``detector._patterns`` with ``candidate``.

    Compiles the candidate set into locals first. In ``strict=True``
    mode a bad pattern makes ``compile_patterns`` raise; we restore the
    previous ``_patterns`` before re-raising so the detector is never
    left holding a poisoned list that re-raises on every future call.
    """
    saved = detector._patterns
    detector._patterns = candidate
    try:
        compiled_all = compile_patterns(detector)
    except Exception:
        detector._patterns = saved  # roll back before re-raising
        raise
    detector._compiled_all = compiled_all
    detector._compiled_no_cursor_end_req = [
        (regex, pat) for (regex, pat) in detector._compiled_all if not bool(pat.get("expect_cursor_at_end", True))
    ]
    detector._compiled = detector._compiled_all

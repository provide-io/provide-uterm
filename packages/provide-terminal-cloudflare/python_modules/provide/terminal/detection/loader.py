#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Loader for RuleSet from various sources."""

from __future__ import annotations

import json
from pathlib import Path

from provide.terminal.detection.rules import RuleSet


def load_ruleset(source: RuleSet | Path | str) -> RuleSet:
    """Load a RuleSet from a Path, JSON string, or pass through an existing RuleSet.

    Raises:
        ValueError: If rules cannot be loaded or parsed.
    """
    if isinstance(source, RuleSet):
        return source
    if isinstance(source, Path):
        if not source.exists():
            raise ValueError(f"Rules file not found: {source}")
        return RuleSet.from_json_file(source)
    try:
        data = json.loads(source)
        return RuleSet.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Failed to parse rules: {exc}") from exc

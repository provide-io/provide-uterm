#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Annotation feature: mark and detect interesting moments in terminal session recordings."""

from __future__ import annotations

from provide.uterm.annotation._detector import PatternDetector
from provide.uterm.annotation._models import Annotation, AnnotationSpan, DetectionRule
from provide.uterm.annotation._rules import BUILTIN_RULES
from provide.uterm.annotation._streaming import StreamingDetector

__all__ = [
    "Annotation",
    "AnnotationSpan",
    "BUILTIN_RULES",
    "DetectionRule",
    "PatternDetector",
    "StreamingDetector",
]

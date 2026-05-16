#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.detection.buffer import BufferManager, ScreenBuffer
from provide.uterm.detection.detector import PromptDetector
from provide.uterm.detection.engine import DetectionEngine
from provide.uterm.detection.extractor import KVExtractor, extract_kv
from provide.uterm.detection.input_type import auto_detect_input_type
from provide.uterm.detection.loader import load_ruleset
from provide.uterm.detection.models import (
    PromptDetection,
    PromptDetectionDiagnostics,
    PromptMatch,
    ScreenSnapshot,
)
from provide.uterm.detection.rules import RuleSet
from provide.uterm.detection.saver import ScreenSaver

__all__ = [
    "BufferManager",
    "DetectionEngine",
    "KVExtractor",
    "PromptDetection",
    "PromptDetectionDiagnostics",
    "PromptDetector",
    "PromptMatch",
    "RuleSet",
    "ScreenBuffer",
    "ScreenSaver",
    "ScreenSnapshot",
    "auto_detect_input_type",
    "extract_kv",
    "load_ruleset",
]

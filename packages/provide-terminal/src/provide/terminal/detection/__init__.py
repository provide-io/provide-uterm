#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.terminal.detection.buffer import BufferManager, ScreenBuffer
from provide.terminal.detection.detector import PromptDetector
from provide.terminal.detection.engine import DetectionEngine
from provide.terminal.detection.extractor import KVExtractor, extract_kv
from provide.terminal.detection.input_type import auto_detect_input_type
from provide.terminal.detection.loader import load_ruleset
from provide.terminal.detection.models import (
    PromptDetection,
    PromptDetectionDiagnostics,
    PromptMatch,
    ScreenSnapshot,
)
from provide.terminal.detection.rules import RuleSet
from provide.terminal.detection.saver import ScreenSaver

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

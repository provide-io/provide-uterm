# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# provide-terminal-detection

Rule-based prompt detection and data extraction engine for terminal screens.
Takes a set of regex rules (loaded from JSON or constructed programmatically),
matches them against terminal snapshots, and extracts structured key-value data.
Supports screen buffering, idle detection, screen saving, and async hook
pipelines for real-time processing.

## Installation

```bash
pip install provide-terminal-detection
```

Requires Python 3.11+. Zero required dependencies.

## Key classes

| Export | Description |
|---|---|
| `DetectionEngine` | High-level engine: load rules, process screens (sync and async), manage hooks |
| `PromptDetector` | Compiled pattern matcher with fingerprint-based deduplication |
| `RuleSet` | Declarative rule container; `to_prompt_patterns()` compiles to regex |
| `load_ruleset(path_or_str)` | Load a `RuleSet` from a JSON file, path, or string |
| `KVExtractor` / `extract_kv()` | Extract key-value pairs from screen text via named regex groups |
| `BufferManager` / `ScreenBuffer` | Ring buffer for recent screens with idle-state detection |
| `ScreenSaver` | Persist screen snapshots to disk for replay or debugging |
| `PromptDetection` | Detection result: prompt_id, input_type, kv_data, idle flag |
| `auto_detect_input_type` | Heuristic input-type classifier for unknown prompts |

## Usage

```python
from provide.terminal.detection import DetectionEngine

engine = DetectionEngine("rules.json", idle_threshold_s=2.0)

snapshot = {"screen": "Command [TL] (?=help)? :", "screen_hash": "abc123"}
result = engine._sync_process_screen(snapshot)
if result:
    print(result.prompt_id, result.kv_data)
```

Async processing with hooks:

```python
async def on_detect(snapshot, detection, buffer, is_idle):
    if detection:
        print(f"Detected: {detection.prompt_id}")

engine.add_hook(on_detect)
result = await engine.process_screen(snapshot)
```

## Key modules

- `provide.terminal.detection.engine` -- `DetectionEngine` (sync + async)
- `provide.terminal.detection.detector` -- `PromptDetector` pattern matching core
- `provide.terminal.detection.rules` -- `RuleSet` model and pattern compilation
- `provide.terminal.detection.loader` -- `load_ruleset()` from JSON/path/string
- `provide.terminal.detection.extractor` -- `KVExtractor`, `extract_kv()`
- `provide.terminal.detection.buffer` -- `BufferManager`, `ScreenBuffer`
- `provide.terminal.detection.models` -- `PromptDetection`, `PromptMatch`, `ScreenSnapshot`

## Links

- [Main repository README](../../README.md)

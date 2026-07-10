#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validate the Python and Go public session/hijack-client APIs against
spec/uterm-api.yaml.

Modeled on provide-telemetry's spec/validate_conformance.py: a canonical YAML
spec lists required symbols; per-language extractors statically parse the
actual source (no import/build required); every `required: true` symbol must
have a same-named-under-the-language's-naming-convention counterpart in both
languages, or this fails.

Usage:
    uv run python spec/validate_conformance.py

Exit code 0 if both languages conform, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _conformance_extractors import get_go_exports, get_python_exports, to_pascal_case

try:
    import yaml
except ImportError:
    print("PyYAML is required: uv run python spec/validate_conformance.py", file=sys.stderr)
    sys.exit(1)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _REPO_ROOT / "spec" / "uterm-api.yaml"


def _load_spec() -> dict[str, object]:
    return yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))


def main() -> int:
    spec = _load_spec()
    categories = spec.get("api", {})
    if not isinstance(categories, dict):
        print("spec/uterm-api.yaml: 'api' must be a mapping of categories", file=sys.stderr)
        return 1

    errors: list[str] = []
    checked = 0
    required_count = 0
    for category, entries in categories.items():
        python_exports = get_python_exports(category)
        go_exports = get_go_exports(category)
        for entry in entries:
            name = entry["name"]
            required = bool(entry.get("required", True))
            checked += 1
            if required:
                required_count += 1

            # kind: type names are literal PascalCase in both languages
            # (Python classes are PascalCase too) -- no transform. kind:
            # function names are snake_case in Python, PascalCase in Go.
            go_name = name if entry.get("kind") == "type" else to_pascal_case(name)
            py_ok = name in python_exports
            go_ok = go_name in go_exports

            if not required:
                continue
            if not py_ok:
                errors.append(f"[{category}] python: missing {name!r}")
            if not go_ok:
                errors.append(f"[{category}] go: missing {go_name!r} (spec name {name!r})")

    noun = "category" if len(categories) == 1 else "categories"
    print(f"checked {checked} spec entries ({required_count} required) across {len(categories)} {noun}")
    if errors:
        print(f"\n{len(errors)} conformance error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("all required symbols present in both languages")
    return 0


if __name__ == "__main__":
    sys.exit(main())

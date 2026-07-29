#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Validate the Python, Go, and C# public session/hijack-client APIs against
spec/uterm-api.yaml.

Modeled on provide-telemetry's spec/validate_conformance.py: a canonical YAML
spec lists required symbols; per-language extractors statically parse the
actual source (no import/build required); every `required: true` symbol must
have a same-named-under-the-language's-naming-convention counterpart in both
languages (python, go, csharp), or this fails.

Usage:
    uv run python spec/validate_conformance.py

Exit code 0 if all languages conform, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _conformance_extractors import get_csharp_exports, get_go_exports, get_python_exports, to_pascal_case

try:
    import yaml
except ImportError:
    print("PyYAML is required: uv run python spec/validate_conformance.py", file=sys.stderr)
    sys.exit(1)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _REPO_ROOT / "spec" / "uterm-api.yaml"


def csharp_satisfies(name: str, exports: set[str]) -> bool:
    """Whether the C# source provides *name*, allowing the ``Async`` suffix.

    C#'s Task-based Asynchronous Pattern requires that suffix on any method
    returning a Task, so an asynchronous registry spells the spec's ``get`` as
    ``GetAsync`` and has not diverged by doing so. Insisting on the bare name
    would be asking the C# port to break its own language's convention to
    satisfy a rule written for Go, which does not have one.
    """
    return name in exports or f"{name}Async" in exports


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
        csharp_exports = get_csharp_exports(category)
        for entry in entries:
            name = entry["name"]
            required = bool(entry.get("required", True))
            checked += 1
            if required:
                required_count += 1

            # kind: type names are literal PascalCase in all languages
            # (Python classes are PascalCase too) -- no transform. kind:
            # function names are snake_case in Python, PascalCase in Go/C#.
            go_name = name if entry.get("kind") == "type" else to_pascal_case(name)
            cs_name = go_name  # C# uses the same PascalCase convention as Go
            py_ok = name in python_exports
            go_ok = go_name in go_exports
            cs_ok = csharp_satisfies(cs_name, csharp_exports)

            if not required:
                continue
            if not py_ok:
                errors.append(f"[{category}] python: missing {name!r}")
            if not go_ok:
                errors.append(f"[{category}] go: missing {go_name!r} (spec name {name!r})")
            if not cs_ok:
                errors.append(f"[{category}] csharp: missing {cs_name!r} (spec name {name!r})")

    noun = "category" if len(categories) == 1 else "categories"
    print(f"checked {checked} spec entries ({required_count} required) across {len(categories)} {noun}")
    if errors:
        print(f"\n{len(errors)} conformance error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("all required symbols present in python, go, and csharp")
    return 0


if __name__ == "__main__":
    sys.exit(main())

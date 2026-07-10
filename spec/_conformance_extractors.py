#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Per-language, per-category symbol extractors for spec/validate_conformance.py.

Unlike provide-telemetry's flat "one __init__.py, one go/ dir" extractors,
provide-uterm's session/hijack-client API is method-level and spread across
multiple files per language (TransportSession's methods live in
transport_session.py; connect_telnet/connect_ws are module functions in
sibling files; HijackClient's methods live in client/hijack.py). Each
category in CATEGORY_SOURCES below declares exactly where to look.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Acronyms that Go capitalizes fully (ANSIScreen, not AnsiScreen) rather than
# title-casing like a normal word. Extend as new categories need it.
_GO_ACRONYMS: dict[str, str] = {
    "ansi": "ANSI",
    "id": "ID",
    "url": "URL",
    "ws": "WS",
}

# category -> {"python": [source, ...], "go": [source, ...]}
# Python source: {"file": <path>, "class": <name>} or {"file": <path>, "module_functions": True}
# Go source:     {"dir": <path>, "type": <name>} or {"dir": <path>, "top_level_functions": True}
CATEGORY_SOURCES: dict[str, dict[str, list[dict[str, object]]]] = {
    "session": {
        "python": [
            {"file": "packages/provide-uterm/src/provide/uterm/transport_session.py", "class": "TransportSession"},
            {"file": "packages/provide-uterm/src/provide/uterm/telnet_session.py", "module_functions": True},
            {"file": "packages/provide-uterm/src/provide/uterm/ws_session.py", "module_functions": True},
        ],
        "go": [
            {"dir": "packages/provide-uterm-go/termsession", "type": "TransportSession"},
            {"dir": "packages/provide-uterm-go/termsession", "top_level_functions": True},
        ],
    },
    "hijack_client": {
        "python": [
            {"file": "packages/provide-uterm-client/src/provide/uterm/client/hijack.py", "class": "HijackClient"},
        ],
        "go": [
            {"dir": "packages/provide-uterm-go/client", "type": "HijackClient"},
        ],
    },
}


def to_pascal_case(snake: str) -> str:
    """Convert a snake_case spec name to the Go PascalCase this repo expects.

    Known acronyms (ansi, id, url, ws) are capitalized fully rather than
    title-cased, matching idiomatic Go (ANSIScreen, not AnsiScreen).
    """
    return "".join(_GO_ACRONYMS.get(part.lower(), part.capitalize()) for part in snake.split("_"))


def _python_class_methods(file_path: Path, class_name: str) -> set[str]:
    """Return {class_name} plus its non-underscore-prefixed method names.

    The class name itself is only included if a matching ClassDef is actually
    found (so a typo'd/renamed class shows up as "missing", not silently
    present). Also picks up simple method aliases
    (``update_seq = screen_change_seq``), which are class-body assignments
    rather than ``def`` statements.
    """
    if not file_path.is_file():
        return set()
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names: set[str] = {class_name}
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                    names.add(item.name)
                elif isinstance(item, ast.Assign):
                    names |= {t.id for t in item.targets if isinstance(t, ast.Name) and not t.id.startswith("_")}
            return names
    return set()


def _python_module_functions(file_path: Path) -> set[str]:
    """Return the non-underscore-prefixed top-level function names in file_path."""
    if not file_path.is_file():
        return set()
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }


def get_python_exports(category: str, repo_root: Path | None = None) -> set[str]:
    """Union every Python source's exported names for the given category."""
    repo_root = repo_root or _REPO_ROOT
    names: set[str] = set()
    for source in CATEGORY_SOURCES.get(category, {}).get("python", []):
        file_path = repo_root / str(source["file"])
        if source.get("module_functions"):
            names |= _python_module_functions(file_path)
        elif "class" in source:
            names |= _python_class_methods(file_path, str(source["class"]))
    return names


def _go_files(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    return [p for p in sorted(dir_path.glob("*.go")) if not p.name.endswith("_test.go")]


def _go_type_methods(dir_path: Path, type_name: str) -> set[str]:
    """Return {type_name} plus exported method names with a receiver of
    type_name (pointer or value).

    type_name is only included if a matching ``type Name ...`` declaration is
    actually found (so a typo'd/renamed type shows up as "missing", not
    silently present).
    """
    method_pattern = re.compile(rf"^func \(\w+ \*?{re.escape(type_name)}\) ([A-Z]\w*)\(", re.MULTILINE)
    type_pattern = re.compile(rf"^type {re.escape(type_name)}\b", re.MULTILINE)
    names: set[str] = set()
    found_type = False
    for go_file in _go_files(dir_path):
        text = go_file.read_text(encoding="utf-8")
        names |= set(method_pattern.findall(text))
        found_type = found_type or bool(type_pattern.search(text))
    if found_type:
        names.add(type_name)
    return names


def _go_top_level_functions(dir_path: Path) -> set[str]:
    """Return exported top-level (no receiver) function names in dir_path."""
    pattern = re.compile(r"^func ([A-Z]\w*)\(", re.MULTILINE)
    names: set[str] = set()
    for go_file in _go_files(dir_path):
        names |= set(pattern.findall(go_file.read_text(encoding="utf-8")))
    return names


def get_go_exports(category: str, repo_root: Path | None = None) -> set[str]:
    """Union every Go source's exported names for the given category."""
    repo_root = repo_root or _REPO_ROOT
    names: set[str] = set()
    for source in CATEGORY_SOURCES.get(category, {}).get("go", []):
        dir_path = repo_root / str(source["dir"])
        if source.get("top_level_functions"):
            names |= _go_top_level_functions(dir_path)
        elif "type" in source:
            names |= _go_type_methods(dir_path, str(source["type"]))
    return names

#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A perimeter module must have a surface mutmut can actually mutate.

mutmut skips a decorated function, and skips an **entire decorated class** --
every method inside it -- via ``mutation/file_mutation.py``::

    if isinstance(node, cst.ClassDef) and len(node.decorators):
        return True

``@dataclass`` is such a decorator. So a perimeter entry whose classes are all
dataclasses generates zero mutants, and the gate reports::

    mutation gate ok: explicitly-targeted file(s) have no mutable surface (0 mutants)

and **passes**. The path list then reads as coverage while measuring nothing.

That was live: ``control_channel_patterns.py`` sat on the perimeter with seven
real methods -- ``register`` and its ``id is not None`` branch, ``unregister``'s
true/false, ``clear``, ``get_all``, ``sync_payload``, ``to_frame_entry`` -- and a
green leg reporting ``0 mutants``. Found on 2026-09-03 by sweeping the state
histograms of a full-perimeter run (``docs/mutmut-survivors-triage.md``).

Note which direction this runs in. ``9bc4dd0c`` moved handlers *out* of
decorators and woke ~2600 mutants at once, which at least showed up as a red
gate. Adding a decorator does the reverse: it silently disables enforcement and
everything stays green. Line coverage will not warn you either -- the file was,
and still is, fully covered by 32 behavioural tests.

This guard is a mirror of mutmut's rule, not mutmut itself, so an upgrade could
in principle drift it. The backstop is the scheduled full-perimeter run, where a
file that has gone inert prints an empty ``{}`` histogram.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Where a ``src/...`` perimeter entry can live. Ordered; first match wins.
_SOURCE_ROOTS = (
    "packages/provide-uterm/src",
    "packages/provide-uterm-platform/src",
    "packages/provide-uterm-server/src",
)

#: Decorators mutmut tolerates on a function, as the sole decorator: they are
#: "predictable and it's easy to set up trampolines for them". Anything else --
#: and any second decorator -- makes the function invisible.
_MUTABLE_SOLE_DECORATORS = frozenset({"staticmethod", "classmethod"})


def _perimeter_paths() -> list[str]:
    config = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [str(entry) for entry in config["tool"]["mutmut"]["source_paths"]]


def _modules_for(entry: str) -> list[Path]:
    """Every module a perimeter entry covers — it may name a file or a package."""
    for root in _SOURCE_ROOTS:
        candidate = REPOSITORY_ROOT / root / entry.removeprefix("src/")
        if candidate.is_dir():
            return sorted(candidate.rglob("*.py"))
        if candidate.is_file():
            return [candidate]
    return []


def _is_skipped(node: ast.FunctionDef | ast.AsyncFunctionDef, inside_decorated_class: bool) -> bool:
    if inside_decorated_class:
        return True
    decorators = node.decorator_list
    if not decorators:
        return False
    sole = decorators[0]
    tolerated = len(decorators) == 1 and isinstance(sole, ast.Name) and sole.id in _MUTABLE_SOLE_DECORATORS
    return not tolerated


def _collect(node: ast.AST, inside_decorated_class: bool, out: list[tuple[str, bool]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect(child, inside_decorated_class or bool(child.decorator_list), out)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append((child.name, _is_skipped(child, inside_decorated_class)))
            _collect(child, inside_decorated_class, out)
        else:
            _collect(child, inside_decorated_class, out)


def functions_in(source: str) -> list[tuple[str, bool]]:
    """``(name, mutmut_will_skip_it)`` for every function defined in *source*."""
    found: list[tuple[str, bool]] = []
    _collect(ast.parse(source), False, found)
    return found


def _inert_modules(entries: list[str]) -> list[str]:
    """Perimeter modules that define functions but none mutmut would mutate."""
    inert: list[str] = []
    for entry in entries:
        for module in _modules_for(entry):
            functions = functions_in(module.read_text(encoding="utf-8"))
            # A module with no functions at all (a Pydantic schema, a re-export
            # shim) genuinely has no mutable surface. That is the shim guard's
            # subject, not this one.
            if not functions:
                continue
            if any(not skipped for _, skipped in functions):
                continue
            skipped_names = ", ".join(name for name, _ in functions)
            inert.append(
                f"{module.relative_to(REPOSITORY_ROOT)} defines {len(functions)} function(s) "
                f"and mutmut can mutate none of them: {skipped_names}"
            )
    return inert


def test_every_perimeter_module_offers_something_to_mutate() -> None:
    """A listed module that generates zero mutants is enforcement theatre."""
    inert = _inert_modules(_perimeter_paths())

    assert not inert, (
        "These perimeter modules pass their leg by having nothing to test. Usually a decorated "
        "class (@dataclass counts) hiding every method; move the logic to module level or drop "
        "the decorator:\n  " + "\n  ".join(inert)
    )


def test_the_check_reports_a_module_hidden_behind_a_decorated_class() -> None:
    """The guard must fail on the state it exists to catch, or it proves nothing.

    This is ``control_channel_patterns.py`` as it stood before 2026-09-03,
    reduced to its shape: real branching methods, all inside a ``@dataclass``.
    """
    source = (
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class Registry:\n"
        "    def register(self, item):\n"
        "        if item.id is not None:\n"
        "            self._items[item.id] = item\n"
    )

    functions = functions_in(source)

    assert functions, "the fixture defines a method, so the collector is broken if this is empty"
    assert all(skipped for _, skipped in functions), functions


def test_a_plain_class_and_the_two_tolerated_decorators_stay_mutable() -> None:
    """Mirrors mutmut's exception list; drift in either direction fails here."""
    assert functions_in("def f():\n    return 1\n") == [("f", False)]
    assert functions_in("class C:\n    def m(self):\n        return 1\n") == [("m", False)]
    assert functions_in("class C:\n    @staticmethod\n    def m():\n        return 1\n") == [("m", False)]
    assert functions_in("class C:\n    @classmethod\n    def m(cls):\n        return 1\n") == [("m", False)]
    # A property is skipped: it breaks the trampoline's signature assignment.
    assert functions_in("class C:\n    @property\n    def m(self):\n        return 1\n") == [("m", True)]
    # Two decorators, even tolerated ones, are skipped — mutmut checks len() == 1.
    assert functions_in("class C:\n    @staticmethod\n    @wraps(x)\n    def m():\n        return 1\n") == [("m", True)]

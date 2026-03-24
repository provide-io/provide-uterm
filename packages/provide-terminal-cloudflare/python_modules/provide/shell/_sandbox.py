#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Restricted Python eval/exec sandbox for ushell.

The sandbox exposes a limited set of builtins to prevent accidental (not
intentional — this is a single-tenant shell) access to filesystem, network,
or process primitives.  Variables persist across ``py`` commands within the
same session via a shared :attr:`Sandbox.namespace` dict.

Security model
--------------
This is a **convenience restriction**, not a security boundary.  A
determined user can escape the sandbox.  The intent is to prevent
*accidental* use of dangerous builtins (``open``, ``__import__``, etc.)
rather than to provide a true isolation layer.
"""

from __future__ import annotations

import base64
import builtins
import datetime
import hashlib
import json
import re
import traceback

# ---------------------------------------------------------------------------
# Safe builtin whitelist
# ---------------------------------------------------------------------------

_SAFE_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        # Type constructors
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "memoryview",
        "object",
        "range",
        "set",
        "slice",
        "str",
        "tuple",
        "type",
        # Numeric / math
        "abs",
        "bin",
        "divmod",
        "hex",
        "max",
        "min",
        "oct",
        "pow",
        "round",
        "sum",
        # Iteration / functional
        "all",
        "any",
        "enumerate",
        "filter",
        "iter",
        "len",
        "map",
        "next",
        "reversed",
        "sorted",
        "zip",
        # Introspection / formatting
        "callable",
        "chr",
        "dir",
        "format",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "isinstance",
        "issubclass",
        "ord",
        "repr",
        "vars",
        # I/O (prints to ushell output via namespace override)
        "print",
        # Sentinel values
        "True",
        "False",
        "None",
        "NotImplemented",
        "Ellipsis",
        # Common exceptions
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "RuntimeError",
        "StopIteration",
        "IndexError",
        "OSError",
        "IOError",
        "NameError",
        "ArithmeticError",
        "ZeroDivisionError",
        "OverflowError",
    }
)

_SAFE_BUILTINS: dict[str, object] = {k: getattr(builtins, k) for k in _SAFE_BUILTIN_NAMES if hasattr(builtins, k)}


class Sandbox:
    """Persistent Python REPL namespace with restricted builtins.

    Variables assigned during one ``run()`` call are available in subsequent
    calls (same session lifetime).

    Args:
        extra: Additional names to inject into the namespace (e.g. ``kv``,
               ``env`` CF bindings for advanced use).
    """

    def __init__(self, extra: dict[str, object] | None = None) -> None:
        self.namespace: dict[str, object] = {"__builtins__": _SAFE_BUILTINS}
        if extra:
            self.namespace.update(extra)
        # Capture print output into a buffer
        self._output: list[str] = []
        self.namespace["print"] = self._print

        self.namespace["json"] = json
        self.namespace["datetime"] = datetime
        self.namespace["re"] = re
        self.namespace["hashlib"] = hashlib
        self.namespace["base64"] = base64

    def _print(self, *args: object, sep: str = " ", end: str = "\n") -> None:
        text = sep.join(str(a) for a in args) + end
        self._output.append(text.replace("\n", "\r\n"))

    def run(self, source: str) -> str:
        """Evaluate or execute *source*.

        Tries ``eval`` first (returns repr of result).  Falls back to
        ``exec`` for statements.  Captures ``print()`` output and any
        exceptions, returning them as terminal-safe strings.
        """
        self._output.clear()
        result_str = ""
        try:
            result = eval(compile(source, "<ushell>", "eval"), self.namespace)  # noqa: S307  # nosec B307
            if result is not None:
                result_str = repr(result) + "\r\n"
        except SyntaxError:
            # Not an expression — try as a statement block.
            try:
                exec(compile(source, "<ushell>", "exec"), self.namespace)  # noqa: S102  # nosec B102
            except Exception:
                result_str = "\x1b[31m" + traceback.format_exc().replace("\n", "\r\n") + "\x1b[0m"
        except Exception:
            result_str = "\x1b[31m" + traceback.format_exc().replace("\n", "\r\n") + "\x1b[0m"

        printed = "".join(self._output)
        self._output.clear()
        return printed + result_str

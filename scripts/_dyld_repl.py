#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Minimal interactive Python REPL for use with DYLD_INSERT_LIBRARIES capture.

Uses only sys.stdout.write() for ALL output (prompt + results) so that every
byte goes through write(1, ...) and is captured by the DYLD interpose hook.
Never imports readline or libedit — those use /dev/tty and escape sequences
that bypass the hook and don't appear in the browser terminal.
"""

from __future__ import annotations

import codeop
import sys
import traceback


def _write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def main() -> None:
    _write("\r\nPython REPL (DYLD capture mode)\n")
    _write(f"Python {sys.version}\n")
    _write('Type "exit()" or Ctrl-D to quit.\n\n')

    local_vars: dict = {}
    buf: list[str] = []

    while True:
        prompt = "... " if buf else ">>> "
        _write(prompt)

        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            _write("\n")
            break

        if not line:  # EOF (readline returns "" on EOF)
            if buf:
                _write("\n")
            break

        line = line.rstrip("\n")

        # Echo input back via write() so DYLD captures it for the browser.
        # PTY ECHO is disabled on the slave; without this, typed characters
        # would be invisible in xterm.js.
        _write(line + "\r\n")

        # Intercept exit/quit without needing to exec
        if not buf and line.strip() in ("exit()", "quit()", "exit", "quit"):
            break

        buf.append(line)
        source = "\n".join(buf)

        # codeop.compile_command returns:
        #   None  → incomplete (need more input)
        #   code  → complete and valid
        #   raises SyntaxError → syntax error (execute to show it)
        try:
            compiled = codeop.compile_command(source, "<stdin>", "single")
        except SyntaxError:
            buf.clear()
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            continue

        if compiled is None:
            # Incomplete block — keep accumulating
            continue

        # Complete statement — execute
        buf.clear()
        if not source.strip():
            continue

        try:
            exec(compiled, local_vars)  # noqa: S102
        except SystemExit:
            break
        except KeyboardInterrupt:
            _write("\nKeyboardInterrupt\n")
        except Exception:
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


if __name__ == "__main__":
    main()

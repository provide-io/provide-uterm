//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package shell is a Go port of the provide-uterm in-terminal shell subsystem
// (provide.uterm.shell). It provides the "ushell" REPL: a keystroke line
// editor, an ANSI output/rendering layer, a command dispatcher, and a
// self-contained SessionConnector implementation.
//
// # Scope and porting decisions
//
// Ported faithfully:
//
//   - LineBuffer (see linebuffer.go) — the xterm.js keystroke protocol from
//     _repl.py: term-frame accumulation, \r / \n / \r\n submit, \x7f / \x08
//     backspace, \x03 Ctrl-C (clears buffer, completes as "\x03"), \x04
//     Ctrl-D (empty-submit / EOF), ESC / CSI / SS3 sequence swallowing, and
//     the printable/tab/max-line rules.
//   - The ANSI output helpers and formatters from _output.py (see output.go):
//     colour constants, PROMPT, BANNER, ErrorMsg/InfoMsg/SuccessMsg/Heading,
//     FmtKV and FmtTable — byte-for-byte identical strings (CRLF line
//     endings).
//   - The command dispatcher and return types from commands/dispatcher.py and
//     commands/types.py (see dispatcher.go, types.go), including routing,
//     case-insensitivity, whitespace stripping, help text, and the exact
//     error/usage strings.
//   - The portable commands cast, fetch, help, kv, render, storage, sessions,
//     env, clear — same command names, argument shapes, help text, and output
//     formats.
//   - terminal/_output.py frame builders (Term, WorkerHello) and
//     terminal/_connector.py (UshellConnector) — the SessionConnector method
//     set, welcome frames, echo/dispatch input handling, animation streaming,
//     flow-pause backpressure, snapshot and clear/set_mode.
//
// Not ported (deliberate):
//
//   - commands/py.py and _sandbox.py implement a restricted *Python* eval/exec
//     sandbox. Executing user Python is not semantically portable to Go, so
//     the "py" command is registered as a stub that returns the dispatcher's
//     standard error frame ("py: unavailable in the Go build ..."). The
//     sandbox's only non-exec safety behaviour is an output-size intent; it
//     imposes no timeout or byte cap that the dispatcher must inherit, so no
//     constant is carried over. See cmd_py.go.
//   - The render palette / SGR / RenderFrame internals that _render.py merely
//     re-exports from provide.uterm.render are already ported in the sibling
//     render package; the render command here consumes render.ImageToANSIFrames
//     rather than re-porting them.
//
// Deviations from the Python are documented at each call site and summarised
// in the porting report; the notable ones are: CF-binding duck typing is
// replaced by the Go interfaces in context.go; fetch uses net/http (http/https
// only) instead of urllib; and the connector's analysis "context_names" line
// replaces the sandbox-namespace line.
package shell

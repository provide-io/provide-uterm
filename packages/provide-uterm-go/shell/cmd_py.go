//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

// cmdPy is a stub for the "py" command. The Python implementation (commands/py.py
// + _sandbox.py) evaluates user-supplied Python via eval/exec in a restricted
// namespace, which has no faithful Go equivalent, so it is deliberately not
// ported (see the package doc). The stub keeps the empty-argument usage error
// and otherwise returns the dispatcher's standard error-frame style, reporting
// that the command is unavailable in the Go build.
func cmdPy(source string) Result {
	if source == "" {
		return textResult(ErrorMsg("usage: py <expr>") + Prompt)
	}
	return textResult(ErrorMsg("py: unavailable in the Go build (Python sandbox not ported)") + Prompt)
}

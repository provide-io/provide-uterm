//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Text-level color downgrade functions.
//
// These are the downgrade counterparts to UpgradeTo256 / UpgradeToTruecolor
// in the ansi package. Both operate on string input / output; the unified
// string / bytes dispatchers live in mode.go.

package colors

// DowngradeTo256 downgrades truecolor SGR sequences in text to xterm-256
// cube codes.
//
// It replaces "\x1b[38;2;R;G;Bm" and "\x1b[48;2;R;G;Bm" runs within SGR
// parameter lists with their nearest xterm-256 palette index equivalents
// (38;5;N / 48;5;N). Non-truecolor SGR and other escape sequences pass
// through unchanged. Idempotent on content that contains no truecolor.
func DowngradeTo256(text string) string {
	return SGRRegexp.ReplaceAllStringFunc(text, func(m string) string {
		return RewriteParams(m[2:len(m)-1], Mode256)
	})
}

// DowngradeTo16 downgrades truecolor SGR sequences in text to base 16-color
// codes.
//
// It uses Euclidean-nearest matching over the canonical BBS 16-color
// palette. Non-truecolor SGR passes through unchanged.
func DowngradeTo16(text string) string {
	return SGRRegexp.ReplaceAllStringFunc(text, func(m string) string {
		return RewriteParams(m[2:len(m)-1], Mode16)
	})
}

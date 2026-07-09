//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Unified color-mode dispatchers — string and byte-slice variants.

package colors

// ColorMode selects the color-mode filter applied by ApplyColorMode.
type ColorMode string

// Supported color modes.
const (
	// ModePassthrough returns data unchanged (zero-cost hot path).
	ModePassthrough ColorMode = "passthrough"
	// Mode256 downgrades truecolor SGR to the xterm-256 cube.
	Mode256 ColorMode = "256"
	// Mode16 downgrades truecolor SGR to the base 16-color palette.
	Mode16 ColorMode = "16"
)

// ApplyColorMode applies a color-mode filter to ANSI text containing SGR
// sequences. Modes other than ModePassthrough and Mode256 downgrade to the
// base 16-color palette (mirroring the Python Literal contract).
func ApplyColorMode(data string, mode ColorMode) string {
	if mode == ModePassthrough {
		return data
	}
	if mode == Mode256 {
		return DowngradeTo256(data)
	}
	return DowngradeTo16(data)
}

// ApplyColorModeBytes applies a color-mode filter to raw bytes containing
// SGR sequences.
//
// The Python implementation decodes bytes as latin-1, runs the SGR regex,
// and re-encodes. Latin-1 maps bytes 0-255 one-to-one onto code points
// 0-255 and the SGR pattern is pure ASCII, so running the regex directly
// over the bytes is byte-for-byte equivalent.
func ApplyColorModeBytes(data []byte, mode ColorMode) []byte {
	if mode == ModePassthrough {
		return data
	}
	target := Mode16
	if mode == Mode256 {
		target = Mode256
	}
	return SGRRegexp.ReplaceAllFunc(data, func(m []byte) []byte {
		return []byte(RewriteParams(string(m[2:len(m)-1]), target))
	})
}

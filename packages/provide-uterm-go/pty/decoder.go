//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"strings"
	"unicode/utf8"
)

// incrementalDecoder is a stateful UTF-8 decoder that mirrors Python's
// codecs.getincrementaldecoder("utf-8")(errors="replace"): os.read can split a
// multibyte sequence at a read boundary, and a naive per-call decode would turn
// each fragment into U+FFFD and permanently corrupt the character. This decoder
// instead holds trailing partial bytes internally and emits the completed
// codepoint on the next Decode call. Genuinely invalid bytes still surface as
// U+FFFD (utf8.RuneError), matching errors="replace".
type incrementalDecoder struct {
	buf []byte // trailing bytes of an as-yet-incomplete sequence
}

// Decode consumes data, returning the text that can be fully decoded now and
// retaining any trailing incomplete sequence for the next call.
func (d *incrementalDecoder) Decode(data []byte) string {
	if len(data) > 0 {
		d.buf = append(d.buf, data...)
	}
	var out strings.Builder
	i := 0
	for i < len(d.buf) {
		c := d.buf[i]
		if c < utf8.RuneSelf {
			out.WriteByte(c)
			i++
			continue
		}
		// A lead byte that begins a still-incomplete (but potentially valid)
		// multibyte sequence: hold it for the next Decode rather than emit a
		// spurious U+FFFD. utf8.FullRune reports an invalid lead byte as a full
		// (width-1 error) rune, so this only pauses on genuine prefixes.
		if !utf8.FullRune(d.buf[i:]) {
			break
		}
		r, size := utf8.DecodeRune(d.buf[i:])
		out.WriteRune(r) // r == utf8.RuneError (U+FFFD) for invalid encodings
		i += size
	}
	// Retain the unconsumed remainder in a fresh backing array so repeated
	// appends do not grow d.buf without bound.
	if i > 0 {
		rem := make([]byte, len(d.buf)-i)
		copy(rem, d.buf[i:])
		d.buf = rem
	}
	return out.String()
}

// decodeReplace decodes complete bytes as UTF-8, substituting U+FFFD for any
// invalid sequence. It is the stateless per-frame analogue of Python's
// bytes.decode("utf-8", errors="replace") used by CaptureConnector (whole
// frames, so no split-boundary handling is required).
func decodeReplace(b []byte) string {
	var sb strings.Builder
	sb.Grow(len(b))
	for len(b) > 0 {
		r, size := utf8.DecodeRune(b)
		sb.WriteRune(r) // r == utf8.RuneError (U+FFFD) for invalid encodings
		b = b[size:]
	}
	return sb.String()
}

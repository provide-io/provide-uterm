//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"strings"
	"unicode/utf8"
)

// UTF8Decoder is a stateful UTF-8 decoder that mirrors Python's
// codecs.getincrementaldecoder("utf-8")(errors="replace"): a read can split a
// multibyte sequence at its boundary, and a naive per-read decode would turn
// each fragment into U+FFFD and permanently corrupt the character. This decoder
// instead holds trailing partial bytes internally and emits the completed
// codepoint on the next Decode call. Genuinely invalid bytes still surface as
// U+FFFD, one for each maximal subpart as Python's errors="replace" emits them.
// The zero value is ready to use.
//
// It sits on every read of a UTF-8 session's emulator, so the common case
// costs what DecodeCP437 does: a read with nothing held whose bytes are valid
// up to a held tail is one allocation, the string itself.
type UTF8Decoder struct {
	// held is the trailing incomplete sequence of the last read, at most
	// three bytes: a fourth would complete or end it.
	held [utf8.UTFMax]byte
	n    int
}

// Decode consumes data, returning the text that can be fully decoded now and
// retaining any trailing incomplete sequence for the next call.
func (d *UTF8Decoder) Decode(data []byte) string {
	if d.n == 0 {
		return d.decodeFrom(data)
	}
	// Finish the held sequence first, from the front of data. It needs at
	// most UTFMax more bytes to complete or to be found ill-formed.
	var joined [2 * utf8.UTFMax]byte
	held := d.n
	m := copy(joined[:], d.held[:held])
	m += copy(joined[m:], data)
	var out [3 * utf8.UTFMax]byte
	o, pos := 0, 0
	for pos < held {
		if !utf8.FullRune(joined[pos:m]) {
			// Still incomplete with all of data added: hold it all.
			d.n = copy(d.held[:], joined[pos:m])
			return string(out[:o])
		}
		r, size := decodeRune(joined[pos:m])
		o += utf8.EncodeRune(out[o:], r)
		pos += size
	}
	d.n = 0
	return string(out[:o]) + d.decodeFrom(data[pos-held:])
}

// decodeFrom decodes data with nothing held, holding its incomplete tail.
func (d *UTF8Decoder) decodeFrom(data []byte) string {
	// The tail is held only when the last lead byte begins a sequence that
	// is still incomplete. Bytes before it are decoded as they are: an
	// ill-formed sequence never runs into a lead byte, so where it ends does
	// not depend on what comes after.
	end := len(data)
	for j := len(data) - 1; j >= 0 && j >= len(data)-utf8.UTFMax; j-- {
		if utf8.RuneStart(data[j]) {
			if !utf8.FullRune(data[j:]) {
				end = j
			}
			break
		}
	}
	d.n = copy(d.held[:], data[end:])
	return DecodeUTF8(data[:end])
}

// DecodeUTF8 decodes complete bytes as UTF-8, substituting U+FFFD for any
// invalid sequence. It is the stateless analogue of Python's
// bytes.decode("utf-8", errors="replace"), for payloads that are whole
// messages rather than a stream: a sequence cut short at the end is replaced,
// not held.
func DecodeUTF8(b []byte) string {
	if utf8.Valid(b) {
		return string(b)
	}
	var sb strings.Builder
	sb.Grow(len(b))
	for len(b) > 0 {
		r, size := decodeRune(b)
		sb.WriteRune(r)
		b = b[size:]
	}
	return sb.String()
}

// decodeRune is utf8.DecodeRune with Python's replacement width. Go reports
// every ill-formed sequence one byte at a time, so "\xe2\x82x" would decode
// to two U+FFFD where Python gives one: Python (like the Unicode standard's
// recommended practice) replaces the longest prefix that could still have
// begun a valid sequence as a single U+FFFD.
func decodeRune(b []byte) (rune, int) {
	r, size := utf8.DecodeRune(b)
	if r == utf8.RuneError && size == 1 {
		size = maximalSubpart(b)
	}
	return r, size
}

// maximalSubpart reports how many bytes at the start of b, an ill-formed
// sequence, are one maximal subpart: the lead byte plus every continuation
// byte that a valid sequence with that lead could have had in that place. The
// second byte's range depends on the lead (it is what rules out overlong
// forms, surrogates and code points past U+10FFFF); later bytes are any
// continuation byte.
func maximalSubpart(b []byte) int {
	lead := b[0]
	width, lo, hi := 1, byte(0x80), byte(0xBF)
	switch {
	case lead >= 0xC2 && lead <= 0xDF:
		width = 2
	case lead == 0xE0:
		width, lo = 3, 0xA0
	case lead == 0xED:
		width, hi = 3, 0x9F
	case lead >= 0xE1 && lead <= 0xEF:
		width = 3
	case lead == 0xF0:
		width, lo = 4, 0x90
	case lead == 0xF4:
		width, hi = 4, 0x8F
	case lead >= 0xF1 && lead <= 0xF3:
		width = 4
	}
	n := 1
	for n < width && n < len(b) && b[n] >= lo && b[n] <= hi {
		lo, hi = 0x80, 0xBF
		n++
	}
	return n
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlchannel

// Lossless byte ↔ string shim for the inline DLE/STX control-frame stream.
//
// The control-frame API is string-typed, but the data carried inside it is
// raw terminal bytes — typically CP437 from a BBS — and must not lose any
// high bytes between the WebSocket boundary and the terminal emulator.
//
// Latin-1 is the shim because it maps bytes 0x00-0xFF to codepoints
// U+0000-U+00FF one-to-one with no replacements. CP437 is *not* a valid shim
// here — it has no codepoint for U+0080-U+009F, so a latin-1→cp437 round-trip
// would silently replace every byte in that range with '?' and destroy
// box-drawing characters. CP437 decoding happens exactly once, inside the
// terminal emulator. Everything upstream stays byte-faithful.

import "strings"

// WSBytesToChannelStr coerces a binary WebSocket frame into the string form
// the Decoder expects. Bytes are decoded as latin-1 so every byte survives as
// a codepoint. Text frames should be passed to the Decoder directly (the
// sender is responsible for not emitting non-latin-1 codepoints into the
// channel).
func WSBytesToChannelStr(raw []byte) string {
	var b strings.Builder
	b.Grow(len(raw))
	for _, c := range raw {
		b.WriteRune(rune(c))
	}
	return b.String()
}

// ChannelStrToBytes recovers raw terminal bytes from a DataChunk.Data string.
//
// Inverse of WSBytesToChannelStr for the data segment. Codepoints above
// U+00FF have no latin-1 representation and are replaced with '?'. The result
// is the original byte stream that should be fed to a terminal emulator
// (which performs its own CP437 decode internally).
func ChannelStrToBytes(data string) []byte {
	out := make([]byte, 0, len(data))
	for _, r := range data {
		if r > 0xFF {
			out = append(out, '?')
		} else {
			out = append(out, byte(r))
		}
	}
	return out
}

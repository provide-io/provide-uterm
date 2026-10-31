//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"strings"
	"testing"
)

// A read the size a busy session hands the emulator: ASCII with SGR, and CJK.
var (
	benchASCII = []byte(strings.Repeat("\x1b[1;32mhello world\x1b[0m 0123456789 ", 100))
	benchCJK   = []byte(strings.Repeat("中文字 café ", 200))
)

func BenchmarkDecodeCP437(b *testing.B) {
	b.ReportAllocs()
	for range b.N {
		_ = DecodeCP437(benchASCII)
	}
}

func BenchmarkUTF8DecoderASCII(b *testing.B) {
	var d UTF8Decoder
	b.ReportAllocs()
	for range b.N {
		_ = d.Decode(benchASCII)
	}
}

func BenchmarkUTF8DecoderCJK(b *testing.B) {
	var d UTF8Decoder
	b.ReportAllocs()
	for range b.N {
		_ = d.Decode(benchCJK)
	}
}

func BenchmarkUTF8DecoderSplitRead(b *testing.B) {
	var d UTF8Decoder
	head, tail := benchCJK[:len(benchCJK)-2], benchCJK[len(benchCJK)-2:]
	b.ReportAllocs()
	for range b.N {
		_ = d.Decode(head)
		_ = d.Decode(tail)
	}
}

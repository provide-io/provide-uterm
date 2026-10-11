// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package gui

import "testing"

// TestZlibRLEDynamicTreeWithEqualLengthRun pins the dynamic-block path where
// sixteen adjacent literals share one code length, so sendTree emits a repeat
// code and must reset its run limits to the "same length continues" pair
// (6, 3). The reference bytes are what CPython's zlib (level 9, Z_RLE,
// windowBits 15, memLevel 8) emits for the same input:
//
//	raw = bytes((97 + (i*7) % 16) for i in range(4096))
func TestZlibRLEDynamicTreeWithEqualLengthRun(t *testing.T) {
	raw := make([]byte, 4096)
	for i := range raw {
		raw[i] = byte(97 + (i*7)%16)
	}
	encoded := zlibCompressRLE(raw)
	if got := (encoded[2] >> 1) & 3; got != 2 {
		t.Fatalf("BTYPE = %d, want 2 (dynamic)", got)
	}
	const wantLen = 2102
	const wantSha = "254c5524b4df48b5adec6aae33e0fbaafb927c17bd128ea38a4fb248988a9867"
	if len(encoded) != wantLen {
		t.Fatalf("length = %d, want %d", len(encoded), wantLen)
	}
	if got := sha256Hex(encoded); got != wantSha {
		t.Fatalf("sha256 = %s, want %s", got, wantSha)
	}
}

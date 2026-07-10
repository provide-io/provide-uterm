//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"strings"
	"testing"
)

func TestIncrementalDecoderASCII(t *testing.T) {
	d := &incrementalDecoder{}
	if got := d.Decode([]byte("hello")); got != "hello" {
		t.Fatalf("got %q", got)
	}
}

func TestIncrementalDecoderThreeByteSplit(t *testing.T) {
	d := &incrementalDecoder{}
	// U+20AC EURO == e2 82 ac, split after the first byte, with surrounding ASCII.
	if got := d.Decode([]byte("a\xe2")); got != "a" {
		t.Fatalf("first decode got %q, want \"a\"", got)
	}
	got := d.Decode([]byte("\x82\xacb"))
	if got != "€b" {
		t.Fatalf("second decode got %q, want %q", got, "€b")
	}
	if strings.ContainsRune("a"+got, '�') {
		t.Fatalf("unexpected U+FFFD")
	}
}

func TestIncrementalDecoderFourByteSplit(t *testing.T) {
	d := &incrementalDecoder{}
	emoji := []byte("\xf0\x9f\x98\x80") // U+1F600
	if got := d.Decode(emoji[:2]); got != "" {
		t.Fatalf("first got %q, want empty", got)
	}
	if got := d.Decode(emoji[2:]); got != "\U0001f600" {
		t.Fatalf("second got %q", got)
	}
}

func TestIncrementalDecoderGenuineGarbage(t *testing.T) {
	d := &incrementalDecoder{}
	out := d.Decode([]byte{0xff}) + d.Decode([]byte{0xfe})
	if out != "��" {
		t.Fatalf("got %q, want two U+FFFD", out)
	}
}

func TestIncrementalDecoderInvalidContinuation(t *testing.T) {
	d := &incrementalDecoder{}
	// 0xE2 lead followed by ASCII 'A' (not a continuation): maximal-subpart
	// replacement for the lead, then the ASCII byte decodes normally.
	got := d.Decode([]byte{0xe2, 'A'})
	if got != "�A" {
		t.Fatalf("got %q", got)
	}
}

func TestDecodeReplace(t *testing.T) {
	if got := decodeReplace([]byte("hi")); got != "hi" {
		t.Fatalf("got %q", got)
	}
	if got := decodeReplace([]byte{0xff}); got != "�" {
		t.Fatalf("got %q", got)
	}
	if got := decodeReplace([]byte("\xe2\x82\xac")); got != "€" {
		t.Fatalf("got %q", got)
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package screen

import (
	"strings"
	"testing"
)

func TestIncrementalDecoderASCII(t *testing.T) {
	d := &UTF8Decoder{}
	if got := d.Decode([]byte("hello")); got != "hello" {
		t.Fatalf("got %q", got)
	}
}

func TestIncrementalDecoderThreeByteSplit(t *testing.T) {
	d := &UTF8Decoder{}
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
	d := &UTF8Decoder{}
	emoji := []byte("\xf0\x9f\x98\x80") // U+1F600
	if got := d.Decode(emoji[:2]); got != "" {
		t.Fatalf("first got %q, want empty", got)
	}
	if got := d.Decode(emoji[2:]); got != "\U0001f600" {
		t.Fatalf("second got %q", got)
	}
}

func TestIncrementalDecoderGenuineGarbage(t *testing.T) {
	d := &UTF8Decoder{}
	out := d.Decode([]byte{0xff}) + d.Decode([]byte{0xfe})
	if out != "��" {
		t.Fatalf("got %q, want two U+FFFD", out)
	}
}

func TestIncrementalDecoderInvalidContinuation(t *testing.T) {
	d := &UTF8Decoder{}
	// 0xE2 lead followed by ASCII 'A' (not a continuation): maximal-subpart
	// replacement for the lead, then the ASCII byte decodes normally.
	got := d.Decode([]byte{0xe2, 'A'})
	if got != "�A" {
		t.Fatalf("got %q", got)
	}
}

func TestDecodeUTF8(t *testing.T) {
	if got := DecodeUTF8([]byte("hi")); got != "hi" {
		t.Fatalf("got %q", got)
	}
	if got := DecodeUTF8([]byte{0xff}); got != "�" {
		t.Fatalf("got %q", got)
	}
	if got := DecodeUTF8([]byte("\xe2\x82\xac")); got != "€" {
		t.Fatalf("got %q", got)
	}
}

// pythonReplaceVectors are bytes.decode("utf-8", errors="replace") results
// from CPython: one U+FFFD per maximal subpart of an ill-formed sequence, not
// one per byte as utf8.DecodeRune reports them.
var pythonReplaceVectors = []struct {
	in, want string
}{
	{"\xe2\x82x", "\ufffdx"},
	{"\xe0\x80", "\ufffd\ufffd"},
	{"\xe0\xa0x", "\ufffdx"},
	{"\xed\xa0\x80", "\ufffd\ufffd\ufffd"},
	{"\xed\x9fx", "\ufffdx"},
	{"\xe1\x80x", "\ufffdx"},
	{"\xee\x80x", "\ufffdx"},
	{"\xef\xbfx", "\ufffdx"},
	{"\xe1\xc0", "\ufffd\ufffd"},
	{"\xe2\x7f", "\ufffd\x7f"},
	{"\xc1\x80", "\ufffd\ufffd"},
	{"\xf0\x8f", "\ufffd\ufffd"},
	{"\xf0\x90\x80x", "\ufffdx"},
	{"\xf1\x80\x80x", "\ufffdx"},
	{"\xf3\xbf\xbfx", "\ufffdx"},
	{"\xf4\x90\x80\x80", "\ufffd\ufffd\ufffd\ufffd"},
	{"\xf4\x8f\xbfx", "\ufffdx"},
	{"\xf5\x80", "\ufffd\ufffd"},
	{"a\xc0\xafb", "a\ufffd\ufffdb"},
	{"\xc2x", "\ufffdx"},
	{"\xdf\xc0", "\ufffd\ufffd"},
	{"\x80\x80", "\ufffd\ufffd"},
	{"\xf0\x9f\x98", "\ufffd"},
	{"\xef\xbf\xbd", "\ufffd"},
}

func TestDecodeUTF8MatchesPython(t *testing.T) {
	for _, v := range pythonReplaceVectors {
		if got := DecodeUTF8([]byte(v.in)); got != v.want {
			t.Errorf("DecodeUTF8(%q) = %q, want %q", v.in, got, v.want)
		}
	}
}

func TestIncrementalDecoderMatchesPythonOnceTheStreamMovesOn(t *testing.T) {
	for _, v := range pythonReplaceVectors {
		d := &UTF8Decoder{}
		// A trailing "|" ends any sequence still held, as the next frame does.
		if got := d.Decode([]byte(v.in)) + d.Decode([]byte("|")); got != v.want+"|" {
			t.Errorf("Decode(%q) = %q, want %q", v.in, got, v.want+"|")
		}
	}
}

// Decoding a stream in pieces gives what decoding it whole does, wherever the
// pieces are cut (as Python's incremental decoder does): every split into two
// and three reads of each Python vector, with ASCII around it, and of mixed
// valid text.
func TestUTF8DecoderAnySplitDecodesAsTheWhole(t *testing.T) {
	inputs := []string{"中é\U0001f600 café", "\xf0\xe2\x82|", "\xe2\x82\xe2\x82\xac|"}
	for _, v := range pythonReplaceVectors {
		inputs = append(inputs, "a"+v.in+"|")
	}
	for _, in := range inputs {
		want := DecodeUTF8([]byte(in))
		for i := 0; i <= len(in); i++ {
			for j := i; j <= len(in); j++ {
				d := &UTF8Decoder{}
				got := d.Decode([]byte(in[:i])) + d.Decode([]byte(in[i:j])) + d.Decode([]byte(in[j:]))
				if got != want {
					t.Fatalf("%q split at %d and %d: got %q, want %q", in, i, j, got, want)
				}
			}
		}
	}
}

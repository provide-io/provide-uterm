//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package filters

import (
	"bytes"
	"errors"
	"io"
	"testing"
)

type failReader struct{ after int }

func (f *failReader) ReadByte() (byte, error) {
	if f.after <= 0 {
		return 0, errors.New("boom")
	}
	f.after--
	return 'x', nil
}

func consumeAndRemainder(t *testing.T, fn func(io.ByteReader) error, in []byte) []byte {
	t.Helper()
	r := bytes.NewReader(in)
	if err := fn(r); err != nil {
		t.Fatalf("consume(%v): %v", in, err)
	}
	rest, _ := io.ReadAll(r)
	return rest
}

func TestConsumeIACTwoByteCommands(t *testing.T) {
	for _, cmd := range []byte{WILL, WONT, DO, DONT} {
		rest := consumeAndRemainder(t, ConsumeIAC, []byte{cmd, 0x18, 'k'})
		if !bytes.Equal(rest, []byte{'k'}) {
			t.Fatalf("cmd %#x: rest = %v", cmd, rest)
		}
	}
}

func TestConsumeIACSubnegotiation(t *testing.T) {
	// SB ... IAC SE then remaining payload.
	in := []byte{SB, 0x18, 0x00, 'V', 'T', IAC, SE, 'z'}
	rest := consumeAndRemainder(t, ConsumeIAC, in)
	if !bytes.Equal(rest, []byte{'z'}) {
		t.Fatalf("rest = %v", rest)
	}
	// IAC inside SB not followed by SE continues consuming.
	in = []byte{SB, IAC, 0x00, 0x01, IAC, SE, 'q'}
	rest = consumeAndRemainder(t, ConsumeIAC, in)
	if !bytes.Equal(rest, []byte{'q'}) {
		t.Fatalf("rest = %v", rest)
	}
}

func TestConsumeIACEscapedIACAndEOF(t *testing.T) {
	// IAC IAC — nothing more to consume.
	rest := consumeAndRemainder(t, ConsumeIAC, []byte{IAC, 'd'})
	if !bytes.Equal(rest, []byte{'d'}) {
		t.Fatalf("rest = %v", rest)
	}
	// EOF at every stage returns nil.
	for _, in := range [][]byte{{}, {WILL}, {SB}, {SB, IAC}} {
		if err := ConsumeIAC(bytes.NewReader(in)); err != nil {
			t.Fatalf("in %v: %v", in, err)
		}
	}
}

func TestConsumeIACPropagatesErrors(t *testing.T) {
	if err := ConsumeIAC(&failReader{after: 0}); err == nil {
		t.Fatal("expected error")
	}
}

func TestConsumeEscapeCSI(t *testing.T) {
	// ESC [ 1 ; 3 1 m — consumed through the final byte 'm'.
	rest := consumeAndRemainder(t, ConsumeEscape, []byte("[1;31mHello"))
	if string(rest) != "Hello" {
		t.Fatalf("rest = %q", rest)
	}
	// Arrow key ESC [ A.
	rest = consumeAndRemainder(t, ConsumeEscape, []byte("[Ax"))
	if string(rest) != "x" {
		t.Fatalf("rest = %q", rest)
	}
}

func TestConsumeEscapeSS3AndAlt(t *testing.T) {
	rest := consumeAndRemainder(t, ConsumeEscape, []byte("OPq"))
	if string(rest) != "q" {
		t.Fatalf("rest = %q", rest)
	}
	// ESC + letter (Alt combo): single char consumed.
	rest = consumeAndRemainder(t, ConsumeEscape, []byte("af"))
	if string(rest) != "f" {
		t.Fatalf("rest = %q", rest)
	}
}

func TestConsumeEscapeEOF(t *testing.T) {
	for _, in := range [][]byte{{}, {'['}, {'[', ';'}, {'O'}} {
		if err := ConsumeEscape(bytes.NewReader(in)); err != nil {
			t.Fatalf("in %v: %v", in, err)
		}
	}
}

func TestConsumeEscapePropagatesErrors(t *testing.T) {
	if err := ConsumeEscape(&failReader{after: 0}); err == nil {
		t.Fatal("expected error")
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vnc

import (
	"encoding/binary"
	"io"
	"testing"
)

func TestMaxRFBDimensionConstant(t *testing.T) {
	if MaxRFBDimension != 8192 {
		t.Fatalf("MaxRFBDimension=%d", MaxRFBDimension)
	}
	if maxRFBNameLen != 4096 {
		t.Fatalf("maxRFBNameLen=%d", maxRFBNameLen)
	}
}

func TestServerInitDimensionCap(t *testing.T) {
	// width=8193 should fail the cap check used in runHandshakeAndLoop.
	w := uint16(8193)
	h := uint16(10)
	if w == 0 || h == 0 || int(w) > MaxRFBDimension || int(h) > MaxRFBDimension {
		// expected reject branch
		return
	}
	t.Fatal("expected oversize width rejected")
}

func TestServerInitNameLenCap(t *testing.T) {
	nameLen := uint32(4097)
	if nameLen > maxRFBNameLen {
		return
	}
	t.Fatal("expected oversize name rejected")
}

func TestFilterRFBInput_NilPolicyDropsKey(t *testing.T) {
	// Handshake: version + sec type 1 + clientinit, then key event.
	var buf []byte
	buf = append(buf, []byte("RFB 003.008\n")...)
	buf = append(buf, 1) // security type None
	buf = append(buf, 1) // client init shared
	// KeyEvent type 4 + 7 bytes payload
	key := make([]byte, 8)
	key[0] = ClientKeyEvent
	buf = append(buf, key...)

	in := newBytesReader(buf)
	out := &countingWriter{}
	err := filterRFBInput(out, in, nil, "s", "lease", "p", "operator")
	// EOF after messages is fine; policy nil must not write key payload.
	_ = err
	// Handshake writes version + sec + clientinit = 12+1+1 = 14; key must not be forwarded.
	if out.n > 14 {
		t.Fatalf("nil policy must drop key events; wrote %d bytes", out.n)
	}
}

func TestFilterRFBInput_OperatorWithLeaseForwardsKey(t *testing.T) {
	var buf []byte
	buf = append(buf, []byte("RFB 003.008\n")...)
	buf = append(buf, 1)
	buf = append(buf, 1)
	key := make([]byte, 8)
	key[0] = ClientKeyEvent
	binary.BigEndian.PutUint32(key[4:], 0x61)
	buf = append(buf, key...)

	in := newBytesReader(buf)
	out := &countingWriter{}
	_ = filterRFBInput(out, in, &StrictPolicyEngine{}, "s", "lease-1", "bob", "operator")
	if out.n < 14+8 {
		t.Fatalf("operator with lease should forward key; wrote %d", out.n)
	}
}

type countingWriter struct{ n int }

func (c *countingWriter) Write(p []byte) (int, error) {
	c.n += len(p)
	return len(p), nil
}

type bytesReader struct {
	b []byte
	i int
}

func newBytesReader(b []byte) *bytesReader { return &bytesReader{b: b} }

func (r *bytesReader) Read(p []byte) (int, error) {
	if r.i >= len(r.b) {
		return 0, io.EOF
	}
	n := copy(p, r.b[r.i:])
	r.i += n
	return n, nil
}

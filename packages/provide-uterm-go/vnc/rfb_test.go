//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package vnc_test

import (
	"encoding/binary"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
)

func TestEncodePointerEvent(t *testing.T) {
	b := vnc.EncodePointerEvent(10, 20, 1)
	if len(b) != 6 || b[0] != vnc.ClientPointerEvent || b[1] != 1 {
		t.Fatalf("header: %v", b)
	}
	if binary.BigEndian.Uint16(b[2:4]) != 10 || binary.BigEndian.Uint16(b[4:6]) != 20 {
		t.Fatalf("coords: %v", b)
	}
	// Clamp extremes
	b = vnc.EncodePointerEvent(-1, 70000, 0)
	if binary.BigEndian.Uint16(b[2:4]) != 0 || binary.BigEndian.Uint16(b[4:6]) != 65535 {
		t.Fatalf("clamp low-x high-y: %v", b)
	}
	b = vnc.EncodePointerEvent(70000, -1, 0)
	if binary.BigEndian.Uint16(b[2:4]) != 65535 || binary.BigEndian.Uint16(b[4:6]) != 0 {
		t.Fatalf("clamp high-x low-y: %v", b)
	}
}

func TestEncodeKeyEvent(t *testing.T) {
	down := vnc.EncodeKeyEvent(0xff0d, true)
	if len(down) != 8 || down[0] != vnc.ClientKeyEvent || down[1] != 1 {
		t.Fatalf("down: %v", down)
	}
	if binary.BigEndian.Uint32(down[4:8]) != 0xff0d {
		t.Fatalf("keysym: %v", down)
	}
	up := vnc.EncodeKeyEvent(0x41, false)
	if up[1] != 0 {
		t.Fatalf("up mask: %v", up)
	}
}

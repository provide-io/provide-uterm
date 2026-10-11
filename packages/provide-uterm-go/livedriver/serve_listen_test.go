//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
)

// TestRunServeSurfacesABindFailure proves a failed bind is returned unchanged
// and nothing is announced: a harness waiting for the handshake must see the
// driver exit, not a ready line for a socket that does not exist.
func TestRunServeSurfacesABindFailure(t *testing.T) {
	boom := errors.New("bind refused")
	orig := listen
	listen = func(network, address string) (net.Listener, error) {
		if network != "tcp" || address != ephemeralLoopback {
			t.Errorf("listen(%q, %q), want tcp on the ephemeral loopback", network, address)
		}
		return nil, boom
	}
	t.Cleanup(func() { listen = orig })

	var out strings.Builder
	err := RunServe(context.Background(), ServeOptions{}, strings.NewReader(""), &out)
	if !errors.Is(err, boom) {
		t.Fatalf("RunServe err = %v, want %v", err, boom)
	}
	if out.Len() != 0 {
		t.Fatalf("nothing may be announced on a bind failure, got %q", out.String())
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"strings"
	"testing"
	"time"
)

// TestShellConnectorSpawnsAndEchoes drives a real local shell in a PTY: it sends
// `echo <marker>` and asserts the marker appears on the emulated screen.
func TestShellConnectorSpawnsAndEchoes(t *testing.T) {
	c, err := newShell("shell-echo", "Echo Shell", map[string]any{"command": []string{"/bin/sh"}})
	if err != nil {
		t.Fatalf("newShell: %v", err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = c.Stop(ctx) }()

	if !c.IsConnected() {
		t.Fatal("shell connector should be connected after Start")
	}

	const marker = "uterm-echo-42"
	if err := c.HandleInput(ctx, "echo "+marker+"\n"); err != nil {
		t.Fatalf("HandleInput: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	found := false
	for time.Now().Before(deadline) {
		if _, err := c.Session().WaitForScreenChange(ctx, 500*time.Millisecond, -1); err != nil {
			t.Fatalf("WaitForScreenChange: %v", err)
		}
		if strings.Contains(c.Snapshot().Screen, marker) {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("marker %q never appeared on screen:\n%s", marker, c.Snapshot().Screen)
	}
}

// TestShellDefaultCommand covers the $SHELL / /bin/sh default argv path.
func TestShellDefaultCommand(t *testing.T) {
	c, err := newShell("shell-default", "Default Shell", nil)
	if err != nil {
		t.Fatalf("newShell: %v", err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if err := c.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}

func TestShellRejectsUnknownConfig(t *testing.T) {
	if _, err := newShell("s", "n", map[string]any{"host": "nope"}); err == nil {
		t.Fatal("shell should reject unknown config key")
	}
}

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

// Each connector's session decodes with the encoding the reference's does: a
// local program's output (pty_capture, shell) as UTF-8, a BBS's (telnet,
// SSH, websocket) as CP437.

func waitForScreenText(t *testing.T, c *transportConnector, want string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !strings.Contains(c.Snapshot().Screen, want) {
		if time.Now().After(deadline) {
			t.Fatalf("%q never reached the screen: %q", want, c.Snapshot().Screen)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestACaptureSessionDecodesUTF8(t *testing.T) {
	sock := captureSock(t, "utf8")
	conn, err := newCapture("cap", "Cap", map[string]any{"socket_path": sock})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := conn.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Stop(ctx) }()
	payload := "中文 café ─"
	// Split inside the first character, as two frames on one connection: the
	// connector's own decoder joins it.
	sendCapture(t, sock, payload[:2], payload[2:])
	waitForScreenText(t, conn, payload)
}

func TestAShellSessionDecodesUTF8(t *testing.T) {
	c, err := newShell("shell-utf8", "Shell", map[string]any{"command": []string{"/bin/sh", "-c", `printf '\344\270\255\303\251!'; sleep 5`}})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Stop(ctx) }()
	waitForScreenText(t, c, "中é!")
}

func TestBBSConnectorsDecodeCP437(t *testing.T) {
	telnet, telnetErr := newTelnet("t", "T", map[string]any{"host": "127.0.0.1", "port": 23})
	ssh, sshErr := newSSH("s", "S", map[string]any{"host": "127.0.0.1", "port": 22, "username": "u", "insecure_no_host_check": true})
	ws, wsErr := newWebSocket("w", "W", map[string]any{"url": "ws://127.0.0.1:1/"})
	for kind, c := range map[string]*transportConnector{"telnet": telnet, "ssh": ssh, "websocket": ws} {
		if c == nil {
			t.Fatalf("%s: %v %v %v", kind, telnetErr, sshErr, wsErr)
		}
		// 0xCD is CP437's double horizontal line, and no character in UTF-8.
		e := c.build().Emulator()
		e.Process([]byte{0xcd})
		if got := e.GetSnapshot().Screen; !strings.HasPrefix(got, "\u2550") {
			t.Fatalf("%s session drew 0xcd as %q", kind, got[:4])
		}
	}
}

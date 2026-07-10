//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"testing"
)

func TestNewPTYConnectorValidation(t *testing.T) {
	cases := []struct {
		name    string
		cfg     map[string]any
		wantErr string
	}{
		{"unknown-key", map[string]any{"command": "/bin/sh", "bogus": 1}, "unknown config keys"},
		{"missing-command", map[string]any{"args": []any{}}, "requires 'command'"},
		{"relative-command", map[string]any{"command": "sh"}, "absolute path"},
		{"bad-username", map[string]any{"command": "/bin/sh", "username": "bad user"}, "invalid character"},
		{"bad-input-mode", map[string]any{"command": "/bin/sh", "input_mode": "root"}, "invalid input_mode"},
		{"bad-env", map[string]any{"command": "/bin/sh", "env": map[string]any{"A=B": "x"}}, "must not contain '='"},
		{"non-string-command", map[string]any{"command": 123}, "must be a string"},
		{"bad-run-as-uid", map[string]any{"command": "/bin/sh", "run_as_uid": "abc"}, "must be an integer"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewPTYConnector("s", "d", tc.cfg)
			assertErr(t, err, tc.wantErr)
		})
	}
}

func TestNewPTYConnectorDefaults(t *testing.T) {
	c, err := NewPTYConnector("s", "d", map[string]any{"command": "/bin/sh"})
	if err != nil {
		t.Fatal(err)
	}
	if c.cols != 80 || c.rows != 24 || c.inputMode != "open" || c.inject {
		t.Fatalf("defaults wrong: cols=%d rows=%d mode=%s inject=%t", c.cols, c.rows, c.inputMode, c.inject)
	}
}

func TestNewPTYConnectorCoercions(t *testing.T) {
	c, err := NewPTYConnector("s", "d", map[string]any{
		"command":    "/bin/sh",
		"args":       []any{"-c", "true"},
		"cols":       float64(120),
		"rows":       int64(48),
		"inject":     true,
		"input_mode": "hijack",
		"run_as_uid": float64(1000),
		"run_as_gid": 1000,
		"env":        map[string]any{"FOO": "bar"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(c.args) != 2 || c.args[0] != "-c" || c.cols != 120 || c.rows != 48 || !c.inject {
		t.Fatalf("coercion wrong: %+v", c)
	}
	if c.runAsUID == nil || *c.runAsUID != 1000 || c.runAsGID == nil || *c.runAsGID != 1000 {
		t.Fatalf("run_as ptrs: %v %v", c.runAsUID, c.runAsGID)
	}
	if c.extraEnv["FOO"] != "bar" {
		t.Fatalf("env: %+v", c.extraEnv)
	}
}

func TestConnectorPAMRequiresRoot(t *testing.T) {
	c, err := NewPTYConnector("s", "d", map[string]any{
		"command": "/bin/sh", "username": "alice", "password": "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	c.geteuid = func() int { return 1000 } // non-root
	assertErr(t, c.Start(context.Background()), "requires the server to run as root")
}

func TestConnectorPAMStubFailsClosed(t *testing.T) {
	c, err := NewPTYConnector("s", "d", map[string]any{
		"command": "/bin/sh", "username": "alice", "password": "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	c.geteuid = func() int { return 0 } // pretend root; PAM stub must still refuse
	assertErr(t, c.Start(context.Background()), "libpam not available")
}

func TestConnectorInjectCaptureLifecycle(t *testing.T) {
	c := makeConn(t, "/bin/echo", []string{"hi"}, map[string]any{"inject": true})
	if err := c.Start(context.Background()); err != nil {
		t.Fatalf("start with inject: %v", err)
	}
	c.mu.Lock()
	tmp := c.captureTmpDir
	haveSock := c.captureSocket != nil
	c.mu.Unlock()
	if tmp == "" || !haveSock {
		t.Fatal("inject should create capture socket + tmpdir")
	}
	if err := c.Stop(context.Background()); err != nil {
		t.Fatalf("stop: %v", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.captureSocket != nil || c.captureTmpDir != "" {
		t.Fatal("stop should release capture resources")
	}
}

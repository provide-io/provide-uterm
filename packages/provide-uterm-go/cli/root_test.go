//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

func TestExecuteHelp(t *testing.T) {
	var out, errw bytes.Buffer
	code := Execute([]string{"--help"}, &out, &errw)
	if code != 0 {
		t.Fatalf("help exit = %d, want 0", code)
	}
	s := out.String()
	// The subcommands must appear in Python declaration order.
	order := []string{"proxy", "listen", "share", "tunnel", "inspect", "watch", "audit", "server"}
	last := -1
	for _, name := range order {
		idx := strings.Index(s, "\n  "+name+" ")
		if idx < 0 {
			t.Fatalf("missing subcommand %q in help:\n%s", name, s)
		}
		if idx < last {
			t.Fatalf("subcommand %q out of order", name)
		}
		last = idx
	}
}

func TestExecuteVersion(t *testing.T) {
	var out, errw bytes.Buffer
	if code := Execute([]string{"--version"}, &out, &errw); code != 0 {
		t.Fatalf("version exit = %d", code)
	}
	if !strings.Contains(out.String(), Version) {
		t.Fatalf("version output missing %q: %s", Version, out.String())
	}
}

func TestExecuteUnknownCommand(t *testing.T) {
	var out, errw bytes.Buffer
	code := Execute([]string{"nope"}, &out, &errw)
	if code == 0 {
		t.Fatal("unknown command should exit non-zero")
	}
	if !strings.Contains(errw.String(), "error:") {
		t.Fatalf("expected error message, got %q", errw.String())
	}
}

func TestExecuteMissingRequiredServer(t *testing.T) {
	// share/tunnel/inspect all require --server; omitting it must fail before any
	// network work (cobra required-flag enforcement).
	cases := [][]string{
		{"share"},
		{"tunnel", "1234"},
		{"inspect", "3000"},
	}
	for _, args := range cases {
		var out, errw bytes.Buffer
		code := Execute(args, &out, &errw)
		if code == 0 {
			t.Fatalf("%v without --server should exit non-zero", args)
		}
		if !strings.Contains(errw.String(), "server") {
			t.Fatalf("%v: expected required-flag error, got %q", args, errw.String())
		}
	}
}

func TestExecuteTunnelRegistrationFailure(t *testing.T) {
	// A --server pointing nowhere makes the registration POST fail; the command
	// must surface an error and exit non-zero (no stub message anymore).
	cases := [][]string{
		{"share", "-s", "http://127.0.0.1:1"},
		{"tunnel", "1234", "-s", "http://127.0.0.1:1"},
		{"inspect", "3000", "-s", "http://127.0.0.1:1"},
	}
	for _, args := range cases {
		var out, errw bytes.Buffer
		if code := Execute(args, &out, &errw); code == 0 {
			t.Fatalf("%v should exit non-zero when the server is unreachable", args)
		}
	}
}

// failingWriter always errors — exercises Execute's write-error branch.
type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errors.New("nope") }

func TestExecuteWriteErrorBranch(t *testing.T) {
	// An erroring command with a failing error-writer must still exit non-zero.
	if code := Execute([]string{"listen", "wss://x"}, &bytes.Buffer{}, failingWriter{}); code == 0 {
		t.Fatal("expected non-zero exit even when error write fails")
	}
}

func TestTokenFileDefault(t *testing.T) {
	if tokenFileDefault() == "" {
		t.Fatal("token file default should be non-empty")
	}
}

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

func TestExecuteStubsExitNonZero(t *testing.T) {
	// listen, watch, and audit are now implemented (see listen.go, watch*.go,
	// audit*.go) and have their own tests; only share/tunnel/inspect remain stubs.
	cases := [][]string{
		{"share", "-s", "https://x"},
		{"tunnel", "1234"},
		{"inspect", "3000"},
	}
	for _, args := range cases {
		var out, errw bytes.Buffer
		code := Execute(args, &out, &errw)
		if code == 0 {
			t.Fatalf("%v should exit non-zero", args)
		}
		if !strings.Contains(errw.String(), "not yet available in the Go build") {
			t.Fatalf("%v: missing stub message, got %q", args, errw.String())
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

func TestStubErrMessage(t *testing.T) {
	if got := (stubErr{cmd: "share"}).Error(); got != "uterm share: not yet available in the Go build" {
		t.Fatalf("unexpected stub message: %q", got)
	}
}

func TestTokenFileDefault(t *testing.T) {
	if tokenFileDefault() == "" {
		t.Fatal("token file default should be non-empty")
	}
}

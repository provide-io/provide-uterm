//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestIsolateDevTokenRedirectsAndCleansUp(t *testing.T) {
	t.Setenv(devTokenPathEnv, "")

	cleanup, err := isolateDevToken()
	if err != nil {
		t.Fatalf("isolateDevToken: %v", err)
	}
	path := os.Getenv(devTokenPathEnv)
	if path == "" {
		t.Fatal("the dev-token path was not redirected")
	}
	if home, herr := os.UserHomeDir(); herr == nil && strings.HasPrefix(path, filepath.Join(home, ".uterm")) {
		t.Fatalf("a harness run must not write into the developer's home: %q", path)
	}
	dir := filepath.Dir(path)
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("throwaway dir missing: %v", err)
	}

	cleanup()
	if got := os.Getenv(devTokenPathEnv); got != "" {
		t.Fatalf("cleanup left %s=%q", devTokenPathEnv, got)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("cleanup left %q behind: %v", dir, err)
	}
}

func TestIsolateDevTokenRespectsAnExplicitSetting(t *testing.T) {
	explicit := filepath.Join(t.TempDir(), "chosen_token")
	t.Setenv(devTokenPathEnv, explicit)

	cleanup, err := isolateDevToken()
	if err != nil {
		t.Fatalf("isolateDevToken: %v", err)
	}
	defer cleanup()
	if got := os.Getenv(devTokenPathEnv); got != explicit {
		t.Fatalf("an explicit %s was overridden: %q", devTokenPathEnv, got)
	}
}

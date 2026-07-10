//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestAuditCmdOK runs `audit verify` end-to-end on a valid chain via Execute.
func TestAuditCmdOK(t *testing.T) {
	path, head := writeChain(t, 3)
	var out, errw bytes.Buffer
	code := Execute([]string{"audit", "verify", path}, &out, &errw)
	if code != 0 {
		t.Fatalf("exit = %d, stderr=%q", code, errw.String())
	}
	if !strings.Contains(out.String(), "OK: 3 records") || !strings.Contains(out.String(), head) {
		t.Errorf("stdout = %q", out.String())
	}
}

// TestAuditCmdTamperedExit1 confirms a broken chain exits 1 with a TAMPERED
// report and no extra "error:" line.
func TestAuditCmdTamperedExit1(t *testing.T) {
	path, _ := writeChain(t, 2)
	raw, _ := os.ReadFile(path)
	_ = os.WriteFile(path, bytes.Replace(raw, []byte("user:alice"), []byte("user:eve00"), 1), 0o600)

	var out, errw bytes.Buffer
	code := Execute([]string{"audit", "verify", path}, &out, &errw)
	if code != 1 {
		t.Fatalf("exit = %d, want 1", code)
	}
	if !strings.Contains(out.String(), "TAMPERED") {
		t.Errorf("stdout = %q", out.String())
	}
	if strings.Contains(errw.String(), "error:") {
		t.Errorf("unexpected error line: %q", errw.String())
	}
}

// TestAuditCmdExpectedHeadXOR rejects one of --expected-seq/--expected-hash
// without the other.
func TestAuditCmdExpectedHeadXOR(t *testing.T) {
	path, _ := writeChain(t, 1)
	var out, errw bytes.Buffer
	code := Execute([]string{"audit", "verify", path, "--expected-seq", "1"}, &out, &errw)
	if code == 0 {
		t.Fatal("expected non-zero exit for lone --expected-seq")
	}
	if !strings.Contains(errw.String(), "must be given together") {
		t.Errorf("stderr = %q", errw.String())
	}
}

// TestAuditCmdExpectedHeadBoth accepts the pair and verifies the head.
func TestAuditCmdExpectedHeadBoth(t *testing.T) {
	path, head := writeChain(t, 2)
	var out, errw bytes.Buffer
	code := Execute([]string{"audit", "verify", path, "--expected-seq", "2", "--expected-hash", head}, &out, &errw)
	if code != 0 {
		t.Fatalf("exit = %d stderr=%q", code, errw.String())
	}
}

// TestAuditCmdMissingFile reports not-found and exits 1.
func TestAuditCmdMissingFile(t *testing.T) {
	var out, errw bytes.Buffer
	code := Execute([]string{"audit", "verify", filepath.Join(t.TempDir(), "nope.log")}, &out, &errw)
	if code != 1 || !strings.Contains(out.String(), "audit log not found") {
		t.Fatalf("exit=%d out=%q", code, out.String())
	}
}

// TestAuditHelpUnchanged confirms the flag surface still renders.
func TestAuditHelpUnchanged(t *testing.T) {
	var out, errw bytes.Buffer
	Execute([]string{"audit", "verify", "--help"}, &out, &errw)
	h := out.String()
	for _, want := range []string{"--expected-seq", "--expected-hash", "PATH"} {
		if !strings.Contains(h, want) {
			t.Errorf("help missing %q:\n%s", want, h)
		}
	}
}

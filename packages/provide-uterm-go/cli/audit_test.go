//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// pySignScript writes a Python-signed audit log using the real AuditChain, then
// prints the head "seq hash" so the Go verifier can assert an exact head match.
// The details deliberately exercise the parity-critical surfaces: float ts, big
// int mono_ns, nested dict/list, and a non-ASCII string (ensure_ascii=False).
const pySignScript = `
import sys
from provide.uterm.server.audit_chain import AuditChain
path = sys.argv[1]
chain = AuditChain(path)
chain.append("session.create", principal="user:alice", session_id="s1", source_ip="10.0.0.1",
             detail={"n": 3, "f": 2.5, "big": 12345678901234567, "nested": {"a": [1, 2.0, "x"]}})
chain.append("session.join", principal="user:böб", session_id="s1", detail={"emoji": "héllo→世界", "flag": True})
chain.append("session.close", principal="user:alice", session_id="s1", detail={})
sys.stdout.write(f"{chain.seq} {chain.last_hash}")
`

// runPySign produces a signed log at path via `uv run python` from the repo
// root, returning the head "seq hash" line. It skips the test when uv is absent.
func runPySign(t *testing.T, path string) string {
	t.Helper()
	if _, err := exec.LookPath("uv"); err != nil {
		t.Skip("uv not available; skipping Python differential parity test")
	}
	repoRoot, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatalf("abs repo root: %v", err)
	}
	scriptPath := filepath.Join(t.TempDir(), "sign.py")
	if err := os.WriteFile(scriptPath, []byte(pySignScript), 0o600); err != nil {
		t.Fatalf("write script: %v", err)
	}
	cmd := exec.Command("uv", "run", "python", scriptPath, path)
	cmd.Dir = repoRoot
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Skipf("uv run failed (env not provisioned): %v\n%s", err, stderr.String())
	}
	return strings.TrimSpace(stdout.String())
}

// TestAuditDifferentialParity is the headline parity proof: a log signed by the
// real Python AuditChain verifies byte-exactly in Go, including its head hash.
func TestAuditDifferentialParity(t *testing.T) {
	logPath := filepath.Join(t.TempDir(), "audit.log")
	head := runPySign(t, logPath)
	parts := strings.SplitN(head, " ", 2)
	if len(parts) != 2 {
		t.Fatalf("unexpected head line %q", head)
	}
	wantHash := parts[1]

	res := verifyAuditLog(logPath, nil)
	if !res.OK {
		t.Fatalf("Go verify of Python-signed log failed: reason=%q firstBad=%v", res.Reason, res.FirstBadSeq)
	}
	if res.Count != 3 {
		t.Errorf("count = %d, want 3", res.Count)
	}
	if res.HeadHash == nil || *res.HeadHash != wantHash {
		t.Fatalf("head hash mismatch: Go=%v Python=%q", res.HeadHash, wantHash)
	}

	// And the head assertion path accepts the Python-reported head.
	if got := verifyAuditLog(logPath, &expectedHead{seq: *res.HeadSeq, hash: wantHash}); !got.OK {
		t.Fatalf("expected-head verify failed: %q", got.Reason)
	}
}

// TestAuditDifferentialTamperDetected proves a single altered byte in a
// Python-signed log is caught by the Go verifier.
func TestAuditDifferentialTamperDetected(t *testing.T) {
	logPath := filepath.Join(t.TempDir(), "audit.log")
	_ = runPySign(t, logPath)

	raw, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	// Flip "alice" → "alicf" in the first record's principal (content tamper).
	tampered := bytes.Replace(raw, []byte("user:alice"), []byte("user:alicf"), 1)
	if bytes.Equal(raw, tampered) {
		t.Fatal("tamper substitution did not change the log")
	}
	if err := os.WriteFile(logPath, tampered, 0o600); err != nil {
		t.Fatalf("write tampered: %v", err)
	}

	res := verifyAuditLog(logPath, nil)
	if res.OK {
		t.Fatal("tampered log verified OK; hash chain did not catch the edit")
	}
	if !strings.Contains(res.Reason, "record hash mismatch") {
		t.Errorf("reason = %q, want a record hash mismatch", res.Reason)
	}
}

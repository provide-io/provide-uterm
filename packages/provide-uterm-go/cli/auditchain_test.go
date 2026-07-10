//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// num builds a json.Number the way the parser would (used to seed test records
// so canonicalPayload renders numbers exactly as Python would).
func num(s string) json.Number { return json.Number(s) }

// signRecord fills record_hash over the payload subset and returns the JSONL
// line plus the computed hash — a self-consistent record the verifier accepts.
func signRecord(t *testing.T, fields map[string]any) (string, string) {
	t.Helper()
	payload, err := canonicalPayload(fields)
	if err != nil {
		t.Fatalf("canonicalPayload: %v", err)
	}
	h := computeRecordHash(payload)
	full := map[string]any{"record_hash": h}
	for k, v := range fields {
		full[k] = v
	}
	line, err := json.Marshal(full)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(line), h
}

// baseFields returns a valid payload-field map for the given seq/prev.
func baseFields(seq int, prev string) map[string]any {
	return map[string]any{
		"seq": num(intToStr(seq)), "ts": num("1720000000.5"), "mono_ns": num("123456789"),
		"action": "session.create", "principal": "user:alice", "session_id": "s1",
		"source_ip": "10.0.0.1", "detail": map[string]any{"k": "v"}, "prev_hash": prev,
	}
}

func intToStr(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var digits []byte
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	if neg {
		return "-" + string(digits)
	}
	return string(digits)
}

// writeChain writes a valid n-record chain and returns (path, headHash).
func writeChain(t *testing.T, n int) (string, string) {
	t.Helper()
	prev := genesisHash
	var lines []string
	for i := 1; i <= n; i++ {
		line, h := signRecord(t, baseFields(i, prev))
		lines = append(lines, line)
		prev = h
	}
	path := filepath.Join(t.TempDir(), "audit.log")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatalf("write chain: %v", err)
	}
	return path, prev
}

func TestVerifyValidChain(t *testing.T) {
	path, head := writeChain(t, 4)
	res := verifyAuditLog(path, nil)
	if !res.OK || res.Count != 4 {
		t.Fatalf("verify failed: ok=%v count=%d reason=%q", res.OK, res.Count, res.Reason)
	}
	if res.HeadHash == nil || *res.HeadHash != head {
		t.Fatalf("head hash = %v, want %s", res.HeadHash, head)
	}
	if res.HeadSeq == nil || *res.HeadSeq != 4 {
		t.Fatalf("head seq = %v, want 4", res.HeadSeq)
	}
}

func TestVerifyMissingFile(t *testing.T) {
	res := verifyAuditLog(filepath.Join(t.TempDir(), "nope.log"), nil)
	if res.OK || res.Reason != "audit log not found" {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyBlankLinesSkipped(t *testing.T) {
	line, _ := signRecord(t, baseFields(1, genesisHash))
	path := filepath.Join(t.TempDir(), "a.log")
	if err := os.WriteFile(path, []byte("\n\n"+line+"\n\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if res := verifyAuditLog(path, nil); !res.OK || res.Count != 1 {
		t.Fatalf("ok=%v count=%d", res.OK, res.Count)
	}
}

func TestVerifyUnparseableLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "a.log")
	if err := os.WriteFile(path, []byte("{not json\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	res := verifyAuditLog(path, nil)
	if res.OK || !strings.HasPrefix(res.Reason, "unparseable line 1") {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyMalformedMissingKey(t *testing.T) {
	f := baseFields(1, genesisHash)
	line, _ := signRecord(t, f)
	// Drop a required key from the emitted line.
	var m map[string]any
	_ = json.Unmarshal([]byte(line), &m)
	delete(m, "action")
	b, _ := json.Marshal(m)
	path := filepath.Join(t.TempDir(), "a.log")
	_ = os.WriteFile(path, append(b, '\n'), 0o600)
	if res := verifyAuditLog(path, nil); res.OK || res.Reason != "malformed record" {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyNonIntegerSeq(t *testing.T) {
	f := baseFields(1, genesisHash)
	f["seq"] = num("1.5") // float seq → not an integer
	line, _ := signRecord(t, f)
	path := filepath.Join(t.TempDir(), "a.log")
	_ = os.WriteFile(path, []byte(line+"\n"), 0o600)
	if res := verifyAuditLog(path, nil); res.OK || res.Reason != "malformed record" {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyNonContiguous(t *testing.T) {
	l1, h1 := signRecord(t, baseFields(1, genesisHash))
	l3, _ := signRecord(t, baseFields(3, h1)) // skips seq 2
	path := filepath.Join(t.TempDir(), "a.log")
	_ = os.WriteFile(path, []byte(l1+"\n"+l3+"\n"), 0o600)
	res := verifyAuditLog(path, nil)
	if res.OK || res.Reason != "non-contiguous sequence" {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
	if res.FirstBadSeq == nil || *res.FirstBadSeq != 3 {
		t.Fatalf("firstBad = %v, want 3", res.FirstBadSeq)
	}
}

func TestVerifyBrokenHashLink(t *testing.T) {
	l1, _ := signRecord(t, baseFields(1, genesisHash))
	l2, _ := signRecord(t, baseFields(2, "deadbeef")) // wrong prev_hash
	path := filepath.Join(t.TempDir(), "a.log")
	_ = os.WriteFile(path, []byte(l1+"\n"+l2+"\n"), 0o600)
	if res := verifyAuditLog(path, nil); res.OK || res.Reason != "broken hash link" {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyRecordHashMismatch(t *testing.T) {
	f := baseFields(1, genesisHash)
	line, _ := signRecord(t, f)
	// Corrupt a field without recomputing record_hash.
	tampered := strings.Replace(line, "user:alice", "user:eve00", 1)
	path := filepath.Join(t.TempDir(), "a.log")
	_ = os.WriteFile(path, []byte(tampered+"\n"), 0o600)
	res := verifyAuditLog(path, nil)
	if res.OK || !strings.Contains(res.Reason, "record hash mismatch") {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyExpectedHeadMismatch(t *testing.T) {
	path, head := writeChain(t, 2)
	if res := verifyAuditLog(path, &expectedHead{seq: 2, hash: head}); !res.OK {
		t.Fatalf("matching head should pass: %q", res.Reason)
	}
	res := verifyAuditLog(path, &expectedHead{seq: 2, hash: "wronghash"})
	if res.OK || !strings.Contains(res.Reason, "head mismatch") {
		t.Fatalf("got ok=%v reason=%q", res.OK, res.Reason)
	}
}

func TestVerifyEmptyLog(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty.log")
	_ = os.WriteFile(path, []byte(""), 0o600)
	res := verifyAuditLog(path, nil)
	if !res.OK || res.Count != 0 || res.HeadSeq != nil {
		t.Fatalf("empty log: ok=%v count=%d head=%v", res.OK, res.Count, res.HeadSeq)
	}
}

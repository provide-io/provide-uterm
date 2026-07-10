//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestAuditEncodeAllEscapes covers every explicit escape case in
// auditEncodeString: quote, backslash, and the \r \b \f control shortcuts (the
// existing suite already exercises \n, \t and \uXXXX).
func TestAuditEncodeAllEscapes(t *testing.T) {
	var b strings.Builder
	if err := auditEncode(&b, "a\"b\\c\rd\be\ff"); err != nil {
		t.Fatalf("encode: %v", err)
	}
	want := `"a\"b\\c\rd\be\ff"`
	if got := b.String(); got != want {
		t.Errorf("escapes = %q, want %q", got, want)
	}
}

// TestAuditEncodeNestedContainerErrors covers the error-propagation branches of
// auditEncodeMap and auditEncodeSlice when a nested value is unencodable.
func TestAuditEncodeNestedContainerErrors(t *testing.T) {
	var b strings.Builder
	if err := auditEncode(&b, map[string]any{"k": struct{}{}}); err == nil {
		t.Error("map with unencodable value should error")
	}
	b.Reset()
	if err := auditEncode(&b, []any{struct{}{}}); err == nil {
		t.Error("slice with unencodable element should error")
	}
}

// TestCanonicalPayloadEncodeError covers canonicalPayload's auditEncode failure
// path when a payload field holds an unencodable value.
func TestCanonicalPayloadEncodeError(t *testing.T) {
	if _, err := canonicalPayload(map[string]any{"detail": struct{}{}}); err == nil {
		t.Error("unencodable detail should fail canonical encoding")
	}
}

// TestParseRecordLineErrors covers the trailing-data and non-object branches.
func TestParseRecordLineErrors(t *testing.T) {
	if _, err := parseRecordLine("{} {}"); err == nil {
		t.Error("trailing data should error")
	}
	if _, err := parseRecordLine("[1,2]"); err == nil {
		t.Error("non-object record should error")
	}
}

// TestRecordSeqInvalid covers the non-Number and out-of-int64-range branches.
func TestRecordSeqInvalid(t *testing.T) {
	if _, ok := recordSeq(map[string]any{"seq": "5"}); ok {
		t.Error("string seq must not parse as integer")
	}
	if _, ok := recordSeq(map[string]any{"seq": num("999999999999999999999999999")}); ok {
		t.Error("out-of-range seq must not parse as int64")
	}
}

// TestVerifyRecordsCanonicalError covers verifyRecords' "malformed record" branch
// when a shape-valid record carries an unencodable payload field.
func TestVerifyRecordsCanonicalError(t *testing.T) {
	rec := baseFields(1, genesisHash)
	rec["detail"] = struct{}{} // present (shape ok) but unencodable
	rec["record_hash"] = "deadbeef"
	res := verifyRecords([]map[string]any{rec}, nil)
	if res.OK || res.Reason != "malformed record" {
		t.Fatalf("verify result = %+v, want malformed record", res)
	}
}

// TestVerifyAuditLogOpenNotDir covers the non-ErrNotExist open-error branch: a
// path whose parent component is a regular file yields ENOTDIR, not ErrNotExist.
func TestVerifyAuditLogOpenNotDir(t *testing.T) {
	base := filepath.Join(t.TempDir(), "logfile")
	if err := os.WriteFile(base, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	res := verifyAuditLog(base+"/child", nil)
	if res.OK || res.Reason == "" {
		t.Fatalf("expected a non-ok result, got %+v", res)
	}
}

// TestVerifyAuditLogScanError covers the scanner.Err branch: opening a directory
// succeeds but reading it fails.
func TestVerifyAuditLogScanError(t *testing.T) {
	res := verifyAuditLog(t.TempDir(), nil)
	if res.OK {
		t.Fatalf("scanning a directory should not succeed, got %+v", res)
	}
}

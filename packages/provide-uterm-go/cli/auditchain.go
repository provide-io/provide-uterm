//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strings"
)

// genesisHash is the prev_hash of the very first record in a chain (64 hex
// zeros — same width as a real sha256 digest). Mirrors audit_chain.GENESIS_HASH.
const genesisHash = "0000000000000000000000000000000000000000000000000000000000000000"

// recordKeys are the fields required on every audit record. Mirrors _RECORD_KEYS.
var recordKeys = []string{
	"seq", "ts", "mono_ns", "action", "principal",
	"session_id", "source_ip", "detail", "prev_hash", "record_hash",
}

// payloadKeys are the fields covered by the record hash (every field EXCEPT the
// record's own hash). Mirrors _canonical_payload.
var payloadKeys = []string{
	"seq", "ts", "mono_ns", "action", "principal",
	"session_id", "source_ip", "detail", "prev_hash",
}

// verifyResult is the outcome of verifying an audit chain. Mirrors VerifyResult.
type verifyResult struct {
	OK          bool
	Count       int
	HeadSeq     *int64
	HeadHash    *string
	FirstBadSeq *int64
	Reason      string
}

// expectedHead is an optional (seq, hash) head assertion.
type expectedHead struct {
	seq  int64
	hash string
}

// computeRecordHash returns the sha256 hex digest of a canonical payload.
func computeRecordHash(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

// canonicalPayload deterministically serializes every payload field of a parsed
// record (record_hash excluded) exactly as CPython's
//
//	json.dumps(subset, sort_keys=True, separators=(",",":"), ensure_ascii=False, default=str)
//
// would. This is THE parity surface: the record hash is a sha256 over these
// bytes, so a single byte of divergence makes a valid log look tampered.
//
// Parity resolution (see auditchain_test.go differential test): the Python
// chain does NOT hash the raw JSONL line — it re-serializes a sorted subset. But
// json.dumps of a value parsed back from the line is byte-identical to the
// line's own rendering (shortest-round-trip float repr is stable), so numbers
// are reproduced by parsing them as json.Number and re-rendering through Python's
// repr() algorithm (pyAuditFloatRepr). default=str never fires here because a
// parsed JSONL value is already JSON-native.
func canonicalPayload(record map[string]any) ([]byte, error) {
	subset := make(map[string]any, len(payloadKeys))
	for _, k := range payloadKeys {
		subset[k] = record[k]
	}
	var b strings.Builder
	if err := auditEncode(&b, subset); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

// verifyAuditLog verifies an on-disk JSONL audit log. Mirrors verify_audit_log.
func verifyAuditLog(path string, head *expectedHead) verifyResult {
	f, err := os.Open(path) //nolint:gosec // path is an operator-supplied audit log
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return verifyResult{Reason: "audit log not found"}
		}
		return verifyResult{Reason: "audit log not found"}
	}
	defer f.Close() //nolint:errcheck // read-only file

	var records []map[string]any
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		rec, perr := parseRecordLine(line)
		if perr != nil {
			bad := int64(lineNo)
			return verifyResult{Count: len(records), FirstBadSeq: &bad, Reason: fmt.Sprintf("unparseable line %d", lineNo)}
		}
		records = append(records, rec)
	}
	if err := scanner.Err(); err != nil {
		return verifyResult{Reason: "audit log not found"}
	}
	return verifyRecords(records, head)
}

// parseRecordLine decodes one JSONL line into a map, preserving number tokens
// as json.Number (Python's json.loads keeps the int/float distinction, which is
// load-bearing for canonical re-serialization).
func parseRecordLine(line string) (map[string]any, error) {
	dec := json.NewDecoder(strings.NewReader(line))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, errors.New("trailing data")
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, errors.New("record is not an object")
	}
	return m, nil
}

// verifyRecords verifies a sequence of parsed records forms an unbroken hash
// chain. Mirrors verify_records: the first record establishes the starting
// sequence; thereafter seq must strictly increment by 1, each prev_hash must
// equal the running hash, and each record_hash must equal the recomputed
// canonical hash. On the FIRST failure a non-ok result is returned.
func verifyRecords(records []map[string]any, head *expectedHead) verifyResult {
	prev := genesisHash
	var expectedSeq int64
	haveExpected := false
	count := 0
	var lastSeq *int64
	var lastHash *string

	for _, record := range records {
		count++
		if res, bad := checkRecordShape(record, count); bad {
			return res
		}
		seq, _ := recordSeq(record)
		if !haveExpected {
			expectedSeq = seq
			haveExpected = true
		}
		if seq != expectedSeq {
			s := seq
			return verifyResult{Count: count, FirstBadSeq: &s, Reason: "non-contiguous sequence"}
		}
		if asStr(record["prev_hash"]) != prev {
			s := seq
			return verifyResult{Count: count, FirstBadSeq: &s, Reason: "broken hash link"}
		}
		payload, err := canonicalPayload(record)
		if err != nil {
			s := seq
			return verifyResult{Count: count, FirstBadSeq: &s, Reason: "malformed record"}
		}
		if computeRecordHash(payload) != asStr(record["record_hash"]) {
			s := seq
			return verifyResult{Count: count, FirstBadSeq: &s, Reason: "record hash mismatch — content altered"}
		}
		rh := asStr(record["record_hash"])
		prev = rh
		sc := seq
		lastSeq, lastHash = &sc, &rh
		expectedSeq++
	}

	if head != nil {
		match := lastSeq != nil && lastHash != nil && *lastSeq == head.seq && *lastHash == head.hash
		if !match {
			return verifyResult{
				Count: count, HeadSeq: lastSeq, HeadHash: lastHash, FirstBadSeq: lastSeq,
				Reason: "head mismatch — log truncated or rolled back",
			}
		}
	}
	return verifyResult{OK: true, Count: count, HeadSeq: lastSeq, HeadHash: lastHash}
}

// checkRecordShape validates a record has all required keys and an integer seq.
func checkRecordShape(record map[string]any, count int) (verifyResult, bool) {
	for _, key := range recordKeys {
		if _, ok := record[key]; !ok {
			return verifyResult{Count: count, FirstBadSeq: seqOf(record), Reason: "malformed record"}, true
		}
	}
	if _, ok := recordSeq(record); !ok {
		return verifyResult{Count: count, FirstBadSeq: seqOf(record), Reason: "malformed record"}, true
	}
	return verifyResult{}, false
}

// recordSeq extracts an integer seq. A float/exponential/boolean seq is not an
// integer (mirrors Python's isinstance(seq, int) and not isinstance(seq, bool)).
func recordSeq(record map[string]any) (int64, bool) {
	n, ok := record["seq"].(json.Number)
	if !ok {
		return 0, false
	}
	s := string(n)
	if strings.ContainsAny(s, ".eE") {
		return 0, false
	}
	v, err := n.Int64()
	if err != nil {
		return 0, false
	}
	return v, true
}

// seqOf best-effort extracts a record's seq for error reporting.
func seqOf(record map[string]any) *int64 {
	if v, ok := recordSeq(record); ok {
		return &v
	}
	return nil
}

func asStr(v any) string {
	s, _ := v.(string)
	return s
}

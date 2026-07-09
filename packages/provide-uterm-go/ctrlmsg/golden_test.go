//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// testdata/signature_corpus.json and testdata/builder_golden.json are produced
// by the real Python builders. Regenerate from the repo root with:
//
//	uv run python <scratch>/gen_ctrlmsg_golden.py \
//	    ctrlmsg/testdata/signature_corpus.json \
//	    ctrlmsg/testdata/builder_golden.json
//
// The corpus is the differential proof that Go's CanonicalJSON + HMAC signing
// reproduces CPython's json.dumps byte-for-byte across ASCII, unicode (BMP +
// astral), control chars, ints and floats (fixed + exponential thresholds).

// decodeWithNumber parses JSON preserving the int/float distinction as
// json.Number, exactly as Python's json.loads distinguishes them.
func decodeWithNumber(t *testing.T, s string) any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(s)))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		t.Fatalf("decode claims_json %q: %v", s, err)
	}
	return v
}

type sigRow struct {
	Subject     string  `json:"subject"`
	Fingerprint string  `json:"fingerprint"`
	Transport   string  `json:"transport"`
	Secret      string  `json:"secret"`
	HasClaims   bool    `json:"has_claims"`
	ClaimsJSON  *string `json:"claims_json"`
	Signature   string  `json:"signature"`
}

func TestSignatureCorpusMatchesCPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/signature_corpus.json")
	if err != nil {
		t.Fatalf("read corpus: %v", err)
	}
	var rows []sigRow
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatalf("parse corpus: %v", err)
	}
	if len(rows) < 200 {
		t.Fatalf("corpus too small: %d rows (want >= 200)", len(rows))
	}

	mismatches := 0
	for i, row := range rows {
		opts := []IdentityOption{
			WithFingerprint(row.Fingerprint),
			WithTransport(row.Transport),
			WithSecret([]byte(row.Secret)),
		}
		if row.HasClaims {
			claims, ok := decodeWithNumber(t, *row.ClaimsJSON).(map[string]any)
			if !ok {
				t.Fatalf("row %d: claims_json is not an object", i)
			}
			opts = append(opts, WithClaims(claims))
		}
		msg, err := MakeIdentity(row.Subject, opts...)
		if err != nil {
			t.Fatalf("row %d: MakeIdentity: %v", i, err)
		}
		got, _ := msg["signature"].(string)
		if got != row.Signature {
			mismatches++
			if mismatches <= 10 {
				claims := "<none>"
				if row.ClaimsJSON != nil {
					claims = *row.ClaimsJSON
				}
				t.Errorf("row %d mismatch subj=%q claims=%s\n  go:     %s\n  python: %s",
					i, row.Subject, claims, got, row.Signature)
			}
		}
	}
	if mismatches > 0 {
		t.Fatalf("%d/%d signature mismatches", mismatches, len(rows))
	}
	t.Logf("verified %d signatures against CPython", len(rows))
}

// builderGoldenCalls maps each golden case name to the Go builder output that
// must be semantically equal to the Python builder's output.
func builderGoldenCalls(t *testing.T) map[string]map[string]any {
	t.Helper()
	must := func(m map[string]any, err error) map[string]any {
		if err != nil {
			t.Fatalf("builder error: %v", err)
		}
		return m
	}
	p := func(v int) *int { return &v }
	s := func(v string) *string { return &v }

	return map[string]map[string]any{
		"identity_default": must(MakeIdentity("user:alice")),
		"identity_full": must(MakeIdentity("user:bob",
			WithClaims(map[string]any{"role": "admin", "org": "acme"}),
			WithFingerprint("SHA256:abc123"), WithTransport("ws"))),
		"identity_signed": must(MakeIdentity("user:alice",
			WithClaims(map[string]any{"scope": []any{"write", "read"}, "role": "admin"}),
			WithFingerprint("SHA256:abc"), WithTransport("ws"), WithSecret([]byte("proxy-secret")))),
		"identity_signed_no_claims": must(MakeIdentity("user:alice",
			WithFingerprint("fp"), WithTransport("ssh"), WithSecret([]byte("proxy-secret")))),
		"identity_unicode_claims": must(MakeIdentity("üser",
			WithClaims(map[string]any{"name": "José", "arrow": "→", "n": 3}), WithSecret([]byte("s")))),
		"identity_empty_claims_signed": must(MakeIdentity("user:x",
			WithClaims(map[string]any{}), WithSecret([]byte("k")))),
		"session_token_min":         must(MakeSessionToken("tok-abc", nil)),
		"session_token_player":      must(MakeSessionToken("tok-xyz", p(42))),
		"session_token_player_zero": must(MakeSessionToken("tok", p(0))),
		"resume_min":                must(MakeResume("resume-tok", nil)),
		"resume_player":             must(MakeResume("resume-tok", p(15))),
		"resume_ok":                 MakeResumeOk(),
		"resume_failed_none":        MakeResumeFailed(nil),
		"resume_failed_reason":      MakeResumeFailed(s("token expired")),
		"resume_failed_empty":       MakeResumeFailed(s("")),
		"link_single":               must(MakeLinkPatterns([]map[string]any{{"pattern": `\bsector\b`, "action": "cmd"}})),
		"link_multi": must(MakeLinkPatterns([]map[string]any{
			{"pattern": "alpha", "action": "cmd"},
			{"pattern": "beta", "action": "url"},
			{"pattern": "gamma", "action": "key"},
		})),
		"link_all_optional": must(MakeLinkPatterns([]map[string]any{{
			"pattern": `\d+`, "action": "url", "id": "p.num", "flags": "gi", "group": 1,
			"payload": "https://example.com/", "hover": "Open link", "class": "external-link",
		}})),
		"link_line_contains": must(MakeLinkPatterns([]map[string]any{
			{"pattern": `\((\d+)\)`, "action": "cmd", "line_contains": "Warps to Sector"},
		})),
		"link_empty":      must(MakeLinkPatterns([]map[string]any{})),
		"presence_min":    MakePresenceUpdate("u1", nil),
		"presence_fields": MakePresenceUpdate("u2", map[string]any{"scroll_line": 42, "cursor_col": 10}),
	}
}

func TestBuilderGoldenAgainstPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/builder_golden.json")
	if err != nil {
		t.Fatalf("read builder golden: %v", err)
	}
	var golden map[string]json.RawMessage
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatalf("parse builder golden: %v", err)
	}
	calls := builderGoldenCalls(t)
	if len(golden) != len(calls) {
		t.Fatalf("golden has %d cases, Go table has %d", len(golden), len(calls))
	}
	for name, goMap := range calls {
		t.Run(name, func(t *testing.T) {
			rawGolden, ok := golden[name]
			if !ok {
				t.Fatalf("golden case %q missing", name)
			}
			var want map[string]any
			if err := json.Unmarshal(rawGolden, &want); err != nil {
				t.Fatalf("parse golden: %v", err)
			}
			// Round-trip the Go map through JSON so number types normalise to
			// float64 on both sides, making the compare key-order- and
			// int/float-representation-insensitive.
			goJSON, err := json.Marshal(goMap)
			if err != nil {
				t.Fatalf("marshal go map: %v", err)
			}
			var got map[string]any
			if err := json.Unmarshal(goJSON, &got); err != nil {
				t.Fatalf("parse go map: %v", err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("mismatch:\n  go:     %s\n  python: %s", goJSON, rawGolden)
			}
		})
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// --- StreamRedactor engine ------------------------------------------------

func TestStreamRedactorEmptyRulesIdentity(t *testing.T) {
	r := NewStreamRedactor(nil)
	mustEqual(t, r.Redact("anything AKIAIOSFODNN7EXAMPLE"), "anything AKIAIOSFODNN7EXAMPLE", "no rules -> identity") // pragma: allowlist secret
}

func TestStreamRedactorAllInvalidRulesIdentity(t *testing.T) {
	// Every rule uses an RE2-incompatible lookahead -> all skipped -> identity.
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `foo(?=bar)`, Replacement: "X"},
		{Pattern: `(?<=a)b`, Replacement: "Y"},
	})
	if r.pattern != nil {
		t.Fatal("all-invalid rule set should leave pattern nil (identity)")
	}
	mustEqual(t, r.Redact("foobar"), "foobar", "all invalid -> identity")
}

func TestStreamRedactorSkipsInvalidKeepsValid(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `bad(?=x)`, Replacement: "[BAD]"}, // skipped (RE2 rejects lookahead)
		{Pattern: `secret`, Replacement: "[OK]"},
	})
	mustEqual(t, r.Redact("bad and secret"), "bad and [OK]", "invalid skipped, valid applied")
}

func TestStreamRedactorSingleReplacementFastPath(t *testing.T) {
	// All rules share one replacement -> single-replacement path. The
	// replacement is literal (no $-expansion of the captured text).
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `foo`, Replacement: "$1-X"},
		{Pattern: `bar`, Replacement: "$1-X"},
	})
	mustEqual(t, r.Redact("foo bar baz"), "$1-X $1-X baz", "single replacement literal")
}

func TestStreamRedactorMultiReplacementBisect(t *testing.T) {
	// Distinct replacements + a rule that itself has nested groups, so the
	// lastindex->rule bisect must skip over inner group indices.
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `a(b)(c)`, Replacement: "[ABC]"},
		{Pattern: `xyz`, Replacement: "[XYZ]"},
	})
	mustEqual(t, r.Redact("abc then xyz"), "[ABC] then [XYZ]", "bisect picks correct rule")
}

func TestStreamRedactorDefaultReplacement(t *testing.T) {
	// An empty Replacement defaults to "[REDACTED]" (RedactionRule model default).
	r := NewStreamRedactor([]RedactionRule{{Pattern: `pw`}})
	mustEqual(t, r.Redact("my pw here"), "my [REDACTED] here", "empty replacement default")
}

func TestStreamRedactorNoMatchMultiPath(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `a`, Replacement: "[A]"},
		{Pattern: `b`, Replacement: "[B]"},
	})
	mustEqual(t, r.Redact("zzz"), "zzz", "multi path no match returns input")
}

// --- redactValue ----------------------------------------------------------

func TestRedactValueNestedAndScalars(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	in := map[string]any{
		"s":    "a sec b",
		"n":    42,
		"f":    1.5,
		"b":    true,
		"none": nil,
		"list": []any{"sec", 7, map[string]any{"k": "sec"}},
	}
	out := redactValue(in, r, 0).(map[string]any)
	mustEqual(t, out["s"].(string), "a [R] b", "string redacted")
	mustEqual(t, out["n"].(int), 42, "int unchanged")
	mustEqual(t, out["f"].(float64), 1.5, "float unchanged")
	mustEqual(t, out["b"].(bool), true, "bool unchanged")
	if out["none"] != nil {
		t.Fatal("nil unchanged")
	}
	lst := out["list"].([]any)
	mustEqual(t, lst[0].(string), "[R]", "list string redacted")
	mustEqual(t, lst[1].(int), 7, "list int unchanged")
	mustEqual(t, lst[2].(map[string]any)["k"].(string), "[R]", "nested map string redacted")
	// Input not mutated.
	mustEqual(t, in["s"].(string), "a sec b", "input not mutated")
}

func TestRedactValueDepthCap(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	// A container AT the depth cap is returned verbatim (not walked), but a
	// string is always redacted regardless of depth.
	deep := map[string]any{"k": "sec"}
	returned := redactValue(deep, r, redactMaxDepth)
	// Same reference returned (not walked) because depth >= cap.
	if !reflect.DeepEqual(returned, deep) || returned.(map[string]any)["k"] != "sec" {
		t.Fatal("container at depth cap should be returned verbatim")
	}
	mustEqual(t, redactValue("sec", r, redactMaxDepth+5).(string), "[R]", "string redacted past cap")
}

// --- redactFrameFields ----------------------------------------------------

func TestRedactFrameFieldsTerm(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `tok`, Replacement: "[T]"}})
	out := redactFrameFields(map[string]any{"type": "term", "data": "a tok b"}, r)
	mustEqual(t, out["data"].(string), "a [T] b", "term data redacted")
}

func TestRedactFrameFieldsSnapshot(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	in := map[string]any{
		"type":            "snapshot",
		"screen":          "sec on screen",
		"raw_tail":        "sec tail",
		"prompt_detected": map[string]any{"prompt_text": "sec prompt", "prompt_id": "p1"},
	}
	out := redactFrameFields(in, r)
	mustEqual(t, out["screen"].(string), "[R] on screen", "screen redacted")
	mustEqual(t, out["raw_tail"].(string), "[R] tail", "raw_tail redacted")
	pd := out["prompt_detected"].(map[string]any)
	mustEqual(t, pd["prompt_text"].(string), "[R] prompt", "prompt text redacted")
	mustEqual(t, pd["prompt_id"].(string), "p1", "prompt id unchanged")
	// Input untouched.
	mustEqual(t, in["screen"].(string), "sec on screen", "input screen not mutated")
}

func TestRedactFrameFieldsSnapshotNonStringRawTail(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	// raw_tail absent / non-string is left as-is (no key added), prompt_detected absent.
	out := redactFrameFields(map[string]any{"type": "snapshot", "screen": "sec", "raw_tail": 9}, r)
	mustEqual(t, out["screen"].(string), "[R]", "screen redacted")
	mustEqual(t, out["raw_tail"].(int), 9, "non-string raw_tail unchanged")
}

func TestRedactFrameFieldsAnalysisVariants(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	// raw as string.
	o1 := redactFrameFields(map[string]any{"type": "analysis", "formatted": "sec f", "raw": "sec r"}, r)
	mustEqual(t, o1["formatted"].(string), "[R] f", "formatted redacted")
	mustEqual(t, o1["raw"].(string), "[R] r", "string raw redacted")
	// raw as map.
	o2 := redactFrameFields(map[string]any{"type": "analysis", "formatted": "x", "raw": map[string]any{"k": "sec"}}, r)
	mustEqual(t, o2["raw"].(map[string]any)["k"].(string), "[R]", "map raw redacted")
	// raw as list.
	o3 := redactFrameFields(map[string]any{"type": "analysis", "formatted": "x", "raw": []any{"sec"}}, r)
	mustEqual(t, o3["raw"].([]any)[0].(string), "[R]", "list raw redacted")
	// raw as scalar (untouched).
	o4 := redactFrameFields(map[string]any{"type": "analysis", "formatted": "x", "raw": 5}, r)
	mustEqual(t, o4["raw"].(int), 5, "scalar raw unchanged")
}

func TestRedactFrameFieldsOtherTypeUnchanged(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	in := map[string]any{"type": "hello", "screen": "sec"}
	out := redactFrameFields(in, r)
	// Same reference returned; not redacted.
	mustEqual(t, out["screen"].(string), "sec", "non-content frame unchanged")
}

func TestRedactFrameFieldsFieldDefaults(t *testing.T) {
	// Absent content field coerces to "" (Python str(msg.get(k,""))).
	r := NewStreamRedactor([]RedactionRule{{Pattern: `sec`, Replacement: "[R]"}})
	out := redactFrameFields(map[string]any{"type": "term"}, r)
	mustEqual(t, out["data"].(string), "", "absent data -> empty")
}

// --- concrete Redactor + gates -------------------------------------------

func TestRedactFrameFieldsConcreteRedactor(t *testing.T) {
	rules := []RedactionRule{{Pattern: `AKIA[0-9A-Z]{16}`, Replacement: "[AWS]"}}
	out := RedactFrameFields(map[string]any{"type": "term", "data": "id AKIAIOSFODNN7EXAMPLE"}, rules)
	mustEqual(t, out["data"].(string), "id [AWS]", "concrete redactor applies rules")
}

func TestOutputPolicyGates(t *testing.T) {
	noop := NoOpOutputPolicyGate{}
	rules, err := noop.GetRedactionRules(bg(), PolicyContext{})
	mustEqual(t, err, nil, "noop no err")
	if len(rules) != 0 {
		t.Fatal("noop gate yields no rules")
	}
	def := DefaultRulesOutputPolicyGate{}
	dr, err := def.GetRedactionRules(bg(), PolicyContext{})
	mustEqual(t, err, nil, "default gate no err")
	mustEqual(t, len(dr), len(DefaultRules()), "default gate yields default rules")
}

// --- end-to-end through the hub seam --------------------------------------

func TestBroadcastRedactsRealSecretViaDefaultRules(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = DefaultRulesOutputPolicyGate{}
		c.Redactor = RedactFrameFields
	})
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)

	err := h.Broadcast(bg(), "w1", map[string]any{
		"type":   "snapshot",
		"screen": "leak AKIAIOSFODNN7EXAMPLE end", // pragma: allowlist secret
	})
	mustEqual(t, err, nil, "broadcast err")
	frame := decodeOneControl(t, a.last())
	mustEqual(t, frame["screen"].(string), "leak [AWS_ACCESS_KEY_REDACTED] end", "AWS key redacted per recipient")
}

func TestGetLastSnapshotRedactsRealSecret(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = DefaultRulesOutputPolicyGate{}
		c.Redactor = RedactFrameFields
	})
	st := NewWorkerTermState()
	st.LastSnapshot = map[string]any{"type": "snapshot", "screen": "gh ghp_1234567890abcdefghijklmnopqrstuvwxAB x"}
	h.registry.Put("w1", st)
	out, err := h.GetLastSnapshot(bg(), "w1", newBrowserWS("r"))
	mustEqual(t, err, nil, "no err")
	mustEqual(t, out["screen"].(string), "gh [GITHUB_TOKEN_REDACTED] x", "read-path redacts GitHub token")
	mustEqual(t, st.LastSnapshot["screen"].(string), "gh ghp_1234567890abcdefghijklmnopqrstuvwxAB x", "stored snapshot untouched") // pragma: allowlist secret
}

// --- differential parity vs Python StreamRedactor -------------------------

type parityCase struct {
	Input            string `json:"input"`
	ExpectedGoSubset string `json:"expected_go_subset"`
	FullPython       string `json:"full_python"`
}

// TestStreamRedactorPythonParity drives a corpus through the Go StreamRedactor
// with the built-in DefaultRules and asserts the output matches the golden
// produced by the Python StreamRedactor. The golden was generated by the Python
// engine restricted to the RE2-compilable rule subset (see
// scripts note below), so it captures exactly what Go produces — proving the
// combine/bisect/skip logic is byte-identical. The FullPython column documents
// where Go diverges: the three lookahead-based generic rules (password/api_key/
// token) that RE2 rejects and Go therefore skips.
//
// Golden regenerated with:
//
//	uv run python scratchpad/gen_redaction_golden.py > hub/testdata/redaction_parity.json
func TestStreamRedactorPythonParity(t *testing.T) {
	raw, err := os.ReadFile("testdata/redaction_parity.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var golden struct {
		Cases []parityCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(golden.Cases) == 0 {
		t.Fatal("golden has no cases")
	}
	r := NewStreamRedactor(DefaultRules())
	sawShared, sawDivergent := false, false
	for i, c := range golden.Cases {
		got := r.Redact(c.Input)
		if got != c.ExpectedGoSubset {
			t.Fatalf("case %d %q: go=%q want=%q", i, c.Input, got, c.ExpectedGoSubset)
		}
		switch {
		case c.ExpectedGoSubset != c.FullPython:
			// A lookahead rule Go skips: Go leaves the input unredacted while
			// full Python redacts it. Pin the documented RE2 divergence.
			sawDivergent = true
			mustEqual(t, got, c.Input, "Go leaves lookahead-rule secret unredacted")
			if c.FullPython == c.Input {
				t.Fatalf("case %d: full Python should differ from input on a divergent rule", i)
			}
		case c.ExpectedGoSubset != c.Input:
			sawShared = true // a shared rule redacted something
		}
	}
	if !sawShared {
		t.Fatal("corpus must exercise at least one shared (RE2-compilable) rule")
	}
	if !sawDivergent {
		t.Fatal("corpus must exercise at least one RE2-divergent lookahead rule")
	}
}

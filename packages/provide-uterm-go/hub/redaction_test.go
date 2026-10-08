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
	// Lookbehind, and a lookahead with pattern after it: neither is a tail
	// lookahead, so RE2 cannot express either -> all skipped -> identity.
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `foo(?=bar)baz`, Replacement: "X"},
		{Pattern: `(?<=a)b`, Replacement: "Y"},
	})
	if r.pattern != nil {
		t.Fatal("all-invalid rule set should leave pattern nil (identity)")
	}
	mustEqual(t, r.Redact("foobarbaz ab"), "foobarbaz ab", "all invalid -> identity")
}

func TestStreamRedactorSkipsInvalidKeepsValid(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `(?<=x)bad`, Replacement: "[BAD]"}, // skipped (RE2 rejects lookbehind)
		{Pattern: `secret`, Replacement: "[OK]"},
	})
	mustEqual(t, r.Redact("xbad and secret"), "xbad and [OK]", "invalid skipped, valid applied")
}

// A lookahead at the very end of a rule is honoured, as Python's re honours
// it: the match must be followed by the lookahead's text, which is checked
// but neither replaced nor consumed. Expected values are CPython's re.sub.
func TestStreamRedactorTailLookahead(t *testing.T) {
	cases := []struct{ pattern, in, want string }{
		{`foo(?=bar)`, "foobar foo", "Xbar foo"},
		// A lazy body extends until the lookahead holds.
		{`a.*?(?=;)`, "ab;cd;", "X;cd;"},
		// The lookahead's text is not consumed: the next match may start in it.
		{`x\d(?=x)`, "x1x2x", "XXx"},
		{`k=(\S+?)(?=,|$)`, "k=v1,k=v2", "X,X"},
		// Inside a group, at the end of the pattern, still counts as the tail.
		{`(?i:pw=([^ ]+?)(?=\s|$))`, "PW=a pw=b", "X X"},
		// A rule that is nothing but a lookahead replaces nothing and inserts
		// its replacement, stepping on past each position, as re.sub does.
		{`(?=x)`, "axbx", "aXxbXx"},
		{`(?=$)`, "ab", "abX"},
		// A ")" in a character class does not close the lookahead early.
		{`a[)](?=b)`, "a)b a)c", "Xb a)c"},
	}
	for _, c := range cases {
		r := NewStreamRedactor([]RedactionRule{{Pattern: c.pattern, Replacement: "X"}})
		mustEqual(t, r.Redact(c.in), c.want, c.pattern)
	}
}

// Rules with and without a lookahead, different replacements, so the rule a
// match belongs to is found past the lookahead's group too.
// What is not a tail lookahead is left to RE2, which rejects it or reads it
// as something else.
func TestTailLookaheadRecognition(t *testing.T) {
	for _, p := range []string{`a\(?=b`, `a(?=b`, `a(?=b)c`, `abc`} {
		if _, ok := tailLookahead(p, "n"); ok {
			t.Errorf("tailLookahead(%q) recognised a tail lookahead", p)
		}
	}
	if got, ok := tailLookahead(`(?i:k=(\w+)(?=;))`, "n"); !ok || got != `(?i:k=(\w+)(?P<n>;))` {
		t.Errorf("tailLookahead rewrote to %q, %t", got, ok)
	}
	// Parentheses in a class or escaped inside the lookahead do not close it.
	if got, ok := tailLookahead(`k(?=[)]|\))`, "n"); !ok || got != `k(?P<n>[)]|\))` {
		t.Errorf("tailLookahead rewrote to %q, %t", got, ok)
	}
}

func TestStreamRedactorTailLookaheadAmongRules(t *testing.T) {
	r := NewStreamRedactor([]RedactionRule{
		{Pattern: `tok=(\w+)(?=;)`, Replacement: "[TOK]"},
		{Pattern: `secret`, Replacement: "[S]"},
	})
	mustEqual(t, r.Redact("tok=abc; secret tok=def"), "[TOK]; [S] tok=def", "lookahead rule among others")
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
// with the built-in DefaultRules and asserts the output matches the Python
// StreamRedactor's. FullPython is the Python engine with every default rule;
// ExpectedGoSubset is the same engine restricted to the rules without a
// lookahead, which is all Go once honoured. The three generic rules
// (password/api_key/token) end in a lookahead, which Go now honours, so Go
// matches the full set; the corpus must still exercise both kinds of rule.
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
	sawShared, sawLookahead := false, false
	for i, c := range golden.Cases {
		got := r.Redact(c.Input)
		if got != c.FullPython {
			t.Fatalf("case %d %q: go=%q python=%q", i, c.Input, got, c.FullPython)
		}
		switch {
		case c.ExpectedGoSubset != c.FullPython:
			sawLookahead = true // only a lookahead rule redacted this
		case c.ExpectedGoSubset != c.Input:
			sawShared = true
		}
	}
	if !sawShared {
		t.Fatal("corpus must exercise at least one rule without a lookahead")
	}
	if !sawLookahead {
		t.Fatal("corpus must exercise at least one lookahead rule")
	}
}

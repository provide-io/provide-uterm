//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"strings"
	"testing"
)

const loginRulesJSON = `{
	"version": "1.0", "game": "test",
	"prompts": [
		{"id": "login.name", "match": {"pattern": "Enter your name", "match_mode": "contains"},
		 "input_type": "multi_key",
		 "kv_extract": [{"field": "attempt", "regex": "Attempt\\s+(\\d+)", "type": "int"}]},
		{"id": "login.password", "match": {"pattern": "Enter password", "match_mode": "contains"},
		 "negative_match": {"pattern": "Enter your name", "match_mode": "contains"},
		 "input_type": "multi_key"},
		{"id": "main.command", "match": {"pattern": "Command [", "match_mode": "contains"},
		 "input_type": "single_key"}
	],
	"flows": [{"id": "login", "description": "login flow", "steps": [
		{"id": "send_name", "kind": "send_keys", "keys": "alice\r",
		 "expects_prompt": "login.name", "gate_prompts": ["login.name"]},
		{"id": "send_password", "kind": "send_keys", "keys": "secret\r",
		 "expects_prompt": "login.password", "gate_prompts": ["login.password"]},
		{"id": "done", "kind": "noop", "expects_prompt": "main.command",
		 "gate_prompts": ["main.command"]}
	]}]
}`

func loginEngine(t *testing.T) *FlowEngine {
	t.Helper()
	rs, err := RuleSetFromJSON([]byte(loginRulesJSON))
	if err != nil {
		t.Fatal(err)
	}
	return NewFlowEngine(rs)
}

func mustAdvance(t *testing.T, fe *FlowEngine, flow, screen string) FlowStep {
	t.Helper()
	step, err := fe.Advance(flow, screen, nil)
	if err != nil {
		t.Fatal(err)
	}
	return step
}

func strDeref(p *string) string {
	if p == nil {
		return "<nil>"
	}
	return *p
}

func TestFlowAdvancesToMatchingAction(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Attempt 3\r\nEnter your name:")
	if step.FlowID != "login" || strDeref(step.CurrentPromptID) != "login.name" ||
		strDeref(step.NextAction) != "alice\r" || step.Done {
		t.Fatalf("step = %+v", step)
	}
	if step.KVData["attempt"] != 3 {
		t.Errorf("kv = %v", step.KVData)
	}
}

func TestFlowHonorsNegativeMatch(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Enter password:")
	if strDeref(step.CurrentPromptID) != "login.password" || strDeref(step.NextAction) != "secret\r" || step.Done {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowTerminalStageDone(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Command [TL=00:00]:")
	if strDeref(step.CurrentPromptID) != "main.command" || step.NextAction != nil || !step.Done {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowPrefersTailPromptOverScrollback(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Enter your name\r\nalice\r\nCommand [TL=00:00]:")
	if strDeref(step.CurrentPromptID) != "main.command" || !step.Done {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowKeepsEarlierStepWhenItIsTail(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Command [TL=00:00]:\r\nx\r\nEnter your name:")
	if strDeref(step.CurrentPromptID) != "login.name" {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowSingleMatchNoop(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "Enter your name:")
	if strDeref(step.CurrentPromptID) != "login.name" {
		t.Fatal("single match")
	}
}

func TestFlowUnknownFlowErrors(t *testing.T) {
	fe := loginEngine(t)
	if _, err := fe.Advance("missing", "screen", nil); err == nil || !strings.Contains(err.Error(), "unknown flow") {
		t.Errorf("err = %v", err)
	}
}

func TestFlowNoMatchingStageReturnsIdle(t *testing.T) {
	fe := loginEngine(t)
	step := mustAdvance(t, fe, "login", "No prompt here")
	if step.FlowID != "login" || step.CurrentPromptID != nil || step.NextAction != nil ||
		step.Done || len(step.KVData) != 0 {
		t.Fatalf("idle step = %+v", step)
	}
}

func TestFlowExplicitCursorReachesSnapshot(t *testing.T) {
	fe := loginEngine(t)
	cursor := [2]int{7, 8}
	snapshot := fe.snapshotFor("Enter your name:", &cursor)
	got := snapshot["cursor"].(map[string]any)
	if got["x"] != 7 || got["y"] != 8 {
		t.Errorf("cursor = %v", got)
	}
	// advance with cursor still works end-to-end
	step, err := fe.Advance("login", "Enter your name:", &cursor)
	if err != nil || strDeref(step.CurrentPromptID) != "login.name" {
		t.Errorf("step = %+v err=%v", step, err)
	}
}

func TestFlowSnapshotDefaults(t *testing.T) {
	fe := loginEngine(t)
	s := fe.snapshotFor("a\nb ", nil)
	if s["screen"] != "a\nb " || s["cursor_at_end"] != true || s["has_trailing_space"] != true {
		t.Errorf("snapshot = %v", s)
	}
	c := s["cursor"].(map[string]any)
	if c["x"] != 0 || c["y"] != 1 {
		t.Errorf("cursor = %v", c)
	}
	if len(s["screen_hash"].(string)) != 64 {
		t.Error("sha256 hash")
	}
	s2 := fe.snapshotFor("xyz", &[2]int{3, 7})
	if s2["has_trailing_space"] != false {
		t.Error("no trailing space")
	}
}

func TestFlowEndAnchoredPromptWithTrailingBlanks(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"version":"1.0","game":"test",
		"prompts":[{"id":"cmd","match":{"pattern":"Command \\[.*\\Z","match_mode":"regex"},"input_type":"single_key"}],
		"flows":[{"id":"f","description":"x","steps":[
			{"id":"done","kind":"noop","expects_prompt":"cmd","gate_prompts":["cmd"]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "f", "Command [TL=00:00]:\n\n")
	if strDeref(step.CurrentPromptID) != "cmd" {
		t.Fatalf("step = %+v", step)
	}
}

func TestMatchPositionEmptyFinditerFallsBack(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"version":"1.0","game":"test",
		"prompts":[{"id":"char_password","match":{"pattern":"password[?:]\\s*$","match_mode":"regex"},"input_type":"multi_key"}],
		"flows":[{"id":"f","description":"x","steps":[
			{"id":"pw","kind":"noop","expects_prompt":"char_password","gate_prompts":["char_password"]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	screen := "What is your name?\nAlpha-Striker\nPassword? ********\n\n[Pause]"
	if got := fe.matchPosition(screen, "char_password"); got != [2]int{len(screen), 0} {
		t.Errorf("fallback position = %v", got)
	}
}

func TestMatchPositionTailMostAndTies(t *testing.T) {
	fe := loginEngine(t)
	// single match: "Command [" spans 3..12
	if got := fe.matchPosition("xx Command [TL]", "main.command"); got != [2]int{12, -3} {
		t.Errorf("single = %v", got)
	}
	// multiple: tail-most (larger end); second spans 12..21
	if got := fe.matchPosition("Command [a]\nCommand [b]", "main.command"); got != [2]int{21, -12} {
		t.Errorf("multi = %v", got)
	}
	// no match -> (len, 0)
	if got := fe.matchPosition("no match", "main.command"); got != [2]int{len("no match"), 0} {
		t.Errorf("none = %v", got)
	}
	// same end prefers earlier start: (15, 0) > (15, -11)
	if got := fe.matchPosition("Enter your name", "login.name"); got != [2]int{15, 0} {
		t.Errorf("anchored = %v", got)
	}
	if !positionGreater([2]int{15, 0}, [2]int{15, -11}) {
		t.Error("tuple ordering")
	}
	// posRegex cache: repeated call reuses compiled regex
	if got := fe.matchPosition("Enter your name", "login.name"); got != [2]int{15, 0} {
		t.Error("cached call")
	}
	// unknown prompt id -> nil regex -> fallback
	if got := fe.matchPosition("screen", "nope"); got != [2]int{len("screen"), 0} {
		t.Errorf("unknown prompt = %v", got)
	}
}

func TestFlowIsTerminalCases(t *testing.T) {
	fe := loginEngine(t)
	keys := "x"
	noop := ActionRule{ID: "n", Kind: "noop"}
	send := ActionRule{ID: "s", Kind: "send_keys", Keys: &keys}
	sendNoKeys := ActionRule{ID: "sn", Kind: "send_keys"}
	if !fe.isTerminal(noop, false) {
		t.Error("noop always terminal")
	}
	if fe.isTerminal(send, true) || fe.isTerminal(send, false) {
		t.Error("send with keys never terminal")
	}
	if !fe.isTerminal(sendNoKeys, true) {
		t.Error("last without keys terminal")
	}
	if fe.isTerminal(sendNoKeys, false) {
		t.Error("non-last without keys not terminal")
	}
}

func TestFlowCandidatePromptIDs(t *testing.T) {
	fe := loginEngine(t)
	e := "e"
	g := "g"
	if got := fe.candidatePromptIDs(ActionRule{GatePrompts: []string{"g"}, ExpectsPrompt: &e}); strings.Join(got, ",") != "g,e" {
		t.Errorf("got %v", got)
	}
	if got := fe.candidatePromptIDs(ActionRule{GatePrompts: []string{"g"}, ExpectsPrompt: &g}); strings.Join(got, ",") != "g" {
		t.Errorf("dedup got %v", got)
	}
	if got := fe.candidatePromptIDs(ActionRule{GatePrompts: []string{"g"}}); strings.Join(got, ",") != "g" {
		t.Errorf("gates only got %v", got)
	}
	if got := fe.candidatePromptIDs(ActionRule{}); len(got) != 0 {
		t.Errorf("empty got %v", got)
	}
}

func TestFlowExpectsPromptWhenGatesEmpty(t *testing.T) {
	modified := strings.Replace(loginRulesJSON, `"gate_prompts": ["login.name"]`, `"gate_prompts": []`, 1)
	rs, err := RuleSetFromJSON([]byte(modified))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "login", "Enter your name:")
	if strDeref(step.CurrentPromptID) != "login.name" || strDeref(step.NextAction) != "alice\r" {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowIgnoresUnknownGatePrompt(t *testing.T) {
	modified := strings.Replace(loginRulesJSON, `"expects_prompt": "login.name", "gate_prompts": ["login.name"]`,
		`"gate_prompts": ["missing.prompt"]`, 1)
	rs, err := RuleSetFromJSON([]byte(modified))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "login", "Enter your name:")
	if step.CurrentPromptID != nil || step.NextAction != nil {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowIgnoresStepWithoutCandidates(t *testing.T) {
	modified := strings.Replace(loginRulesJSON, `"expects_prompt": "login.name", "gate_prompts": ["login.name"]`,
		`"gate_prompts": []`, 1)
	rs, err := RuleSetFromJSON([]byte(modified))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "login", "Enter your name:")
	if step.CurrentPromptID != nil || step.NextAction != nil {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowTerminalLastStepWithoutKeys(t *testing.T) {
	modified := strings.Replace(loginRulesJSON, `"id": "done", "kind": "noop"`, `"id": "done", "kind": "wait"`, 1)
	rs, err := RuleSetFromJSON([]byte(modified))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	cursor := [2]int{3, 4}
	step, err := fe.Advance("login", "Command [TL=00:00]:", &cursor)
	if err != nil {
		t.Fatal(err)
	}
	if strDeref(step.CurrentPromptID) != "main.command" || step.NextAction != nil || !step.Done {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowWaitActionNotSendable(t *testing.T) {
	modified := strings.Replace(loginRulesJSON,
		`{"id": "send_name", "kind": "send_keys", "keys": "alice\r",`,
		`{"id": "send_name", "kind": "wait", "keys": "ignored",`, 1)
	rs, err := RuleSetFromJSON([]byte(modified))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "login", "Enter your name:")
	if strDeref(step.CurrentPromptID) != "login.name" || step.NextAction != nil || step.Done {
		t.Fatalf("step = %+v", step)
	}
}

func TestFlowPositionTieKeepsEarliestStep(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"version":"1.0","game":"test",
		"prompts":[
			{"id":"p0","match":{"pattern":"DUP","match_mode":"contains"},"input_type":"single_key"},
			{"id":"p1","match":{"pattern":"DUP","match_mode":"contains"},"input_type":"single_key"}],
		"flows":[{"id":"f","description":"x","steps":[
			{"id":"s0","kind":"send_keys","keys":"0\r","gate_prompts":["p0"]},
			{"id":"s1","kind":"send_keys","keys":"1\r","gate_prompts":["p1"]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "f", "DUP")
	if strDeref(step.CurrentPromptID) != "p0" || strDeref(step.NextAction) != "0\r" {
		t.Fatalf("tie step = %+v", step)
	}
}

func TestFlowRanksByPositionNotStepIndex(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"version":"1.0","game":"test",
		"prompts":[
			{"id":"p0","match":{"pattern":"ZZZ","match_mode":"contains"},"input_type":"single_key"},
			{"id":"p1","match":{"pattern":"WWW","match_mode":"contains"},"input_type":"single_key"}],
		"flows":[{"id":"f","description":"x","steps":[
			{"id":"s0","kind":"send_keys","keys":"0\r","gate_prompts":["p0"]},
			{"id":"s1","kind":"send_keys","keys":"1\r","gate_prompts":["p1"]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "f", "xxWWWxxxxZZZ")
	if strDeref(step.CurrentPromptID) != "p0" || strDeref(step.NextAction) != "0\r" {
		t.Fatalf("position step = %+v", step)
	}
}

func TestFlowAnchoredOverSuffix(t *testing.T) {
	rs, err := RuleSetFromJSON([]byte(`{
		"version":"1.0","game":"test",
		"prompts":[
			{"id":"anchored","match":{"pattern":"Enter your password:\\s*$","match_mode":"regex"},"input_type":"multi_key"},
			{"id":"suffix","match":{"pattern":"password[?:]\\s*$","match_mode":"regex"},"input_type":"multi_key"}],
		"flows":[{"id":"f","description":"x","steps":[
			{"id":"s0","kind":"send_keys","keys":"anchored\r","gate_prompts":["anchored"]},
			{"id":"s1","kind":"send_keys","keys":"suffix\r","gate_prompts":["suffix"]}]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	fe := NewFlowEngine(rs)
	step := mustAdvance(t, fe, "f", "Enter your password: ")
	if strDeref(step.CurrentPromptID) != "anchored" || strDeref(step.NextAction) != "anchored\r" {
		t.Fatalf("anchored step = %+v", step)
	}
}

func TestFlowDetectorCacheReused(t *testing.T) {
	// Preserves the perf behavior of commit 1ff5d8d4: one detector per
	// prompt-id set, reused across Advance calls.
	fe := loginEngine(t)
	mustAdvance(t, fe, "login", "Enter your name:")
	if len(fe.detectorCache) == 0 {
		t.Fatal("cache empty after advance")
	}
	before := map[string]*PromptDetector{}
	for k, v := range fe.detectorCache {
		before[k] = v
	}
	mustAdvance(t, fe, "login", "Enter your name:")
	mustAdvance(t, fe, "login", "Enter password:")
	for k, v := range before {
		if fe.detectorCache[k] != v {
			t.Errorf("detector for %q was rebuilt", k)
		}
	}
	// steady-state: cache size equals the number of distinct prompt-id sets (3 steps)
	if len(fe.detectorCache) != 3 {
		t.Errorf("cache size = %d", len(fe.detectorCache))
	}
}

func TestFlowDetectPromptEmptyIDs(t *testing.T) {
	fe := loginEngine(t)
	if fe.detectPrompt(fe.snapshotFor("x", nil), nil) != nil {
		t.Error("empty prompt ids")
	}
	// all-unknown ids -> nil, and not cached as a detector
	if fe.detectPrompt(fe.snapshotFor("x", nil), []string{"nope"}) != nil {
		t.Error("unknown ids")
	}
	if _, cached := fe.detectorCache["nope"]; cached {
		t.Error("unknown-id set must not be cached")
	}
}

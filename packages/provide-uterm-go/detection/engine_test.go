//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"errors"
	"strings"
	"testing"
)

const simpleRulesJSON = `{"version":"1.0","game":"test","prompts":[
	{"id":"prompt.hello","match":{"pattern":"Hello there","match_mode":"contains"},"input_type":"single_key"}]}`

const kvRulesJSON = `{"version":"1.0","game":"test","prompts":[
	{"id":"prompt.sector","match":{"pattern":"Sector\\s+\\d+\\s*:","match_mode":"regex"},"input_type":"single_key",
	 "kv_extract":[
		{"field":"sector","regex":"Sector\\s+(\\d+)","type":"int"},
		{"field":"credits","regex":"Credits:\\s+([\\d,]+)","type":"int"}]}]}`

func newEngine(t *testing.T, rules string, opts ...EngineOption) *DetectionEngine {
	t.Helper()
	e, err := NewDetectionEngine(rules, opts...)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func TestEngineInit(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	if e.PatternCount() != 1 || !e.Enabled() {
		t.Error("init")
	}
	rsEngine, err := NewDetectionEngine(&RuleSet{Game: "t", Version: "1.0"})
	if err != nil || rsEngine.PatternCount() != 0 {
		t.Error("ruleset init")
	}
	if _, err := NewDetectionEngine("not json"); err == nil {
		t.Error("bad rules must error")
	}
}

func TestEngineSyncProcessScreen(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	result := e.SyncProcessScreen(snap("Hello there"))
	if result == nil || result.PromptID != "prompt.hello" {
		t.Fatalf("result = %+v", result)
	}
	if result.Match == nil || result.Match.PromptID != result.PromptID {
		t.Error("match metadata")
	}
	if len(result.KVData) != 0 {
		t.Error("kv empty without config")
	}
	if e.SyncProcessScreen(snap("Goodbye")) != nil {
		t.Error("no match")
	}
}

func TestEngineDisabled(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	e.SetEnabled(false)
	if e.Enabled() || e.SyncProcessScreen(snap("Hello there")) != nil {
		t.Error("disabled engine")
	}
	e.SetEnabled(true)
	if !e.Enabled() {
		t.Error("re-enable")
	}
}

func TestEngineFingerprintCache(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	s := snap("Hello there")
	r1 := e.SyncProcessScreen(s)
	r2 := e.SyncProcessScreen(s)
	if r1 == nil || r2 == nil || r1.PromptID != r2.PromptID {
		t.Error("cache hit")
	}
	// cached negative result also honored on the repeat call
	miss := snap("Something else")
	if e.SyncProcessScreen(miss) != nil {
		t.Error("cache invalidated on new screen")
	}
	if e.SyncProcessScreen(miss) != nil {
		t.Error("cached negative result honored")
	}
}

func TestEngineReloadRules(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	newRules := `{"version":"1.0","game":"t","prompts":[
		{"id":"p.a","match":{"pattern":"A","match_mode":"contains"},"input_type":"single_key"},
		{"id":"p.b","match":{"pattern":"B","match_mode":"contains"},"input_type":"single_key"}]}`
	if err := e.ReloadRules(newRules); err != nil {
		t.Fatal(err)
	}
	if e.PatternCount() != 2 {
		t.Error("reload count")
	}
	// transactional: bad reload keeps old rules
	if err := e.ReloadRules("invalid"); err == nil {
		t.Fatal("expected reload error")
	}
	if e.PatternCount() != 2 {
		t.Error("old rules preserved")
	}
}

func TestEngineReloadClearsFingerprintCache(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	e.SyncProcessScreen(snap("Hello there"))
	byeRules := `{"version":"1.0","game":"t","prompts":[
		{"id":"p.bye","match":{"pattern":"Goodbye","match_mode":"contains"},"input_type":"single_key"}]}`
	if err := e.ReloadRules(byeRules); err != nil {
		t.Fatal(err)
	}
	if e.SyncProcessScreen(snap("Goodbye")) == nil {
		t.Error("new rules apply after reload")
	}
}

func TestEngineNormalizerAndDetector(t *testing.T) {
	e := newEngine(t, simpleRulesJSON, WithEngineNormalizer(strings.ToUpper))
	if e.Detector() == nil {
		t.Error("detector accessor")
	}
	diag := e.DetectWithDiagnostics(snap("Hello there"))
	if diag.Match == nil || diag.Match.PromptID != "prompt.hello" {
		t.Error("diagnostics")
	}
	// reload preserves the normalizer without error
	if err := e.ReloadRules(simpleRulesJSON); err != nil {
		t.Fatal(err)
	}
}

func TestEngineKVExtraction(t *testing.T) {
	e := newEngine(t, kvRulesJSON)
	result := e.SyncProcessScreen(snap("Sector 42 : Credits: 15,000"))
	if result == nil {
		t.Fatal("no result")
	}
	if result.KVData["sector"] != 42 || result.KVData["credits"] != 15000 {
		t.Errorf("kv = %v", result.KVData)
	}
	// kv configured but nothing extractable -> kv_data stays empty
	noKV := `{"version":"1.0","game":"t","prompts":[
		{"id":"prompt.hello","match":{"pattern":"Hello there","match_mode":"contains"},"input_type":"single_key",
		 "kv_extract":[{"field":"score","regex":"Score:\\s*(\\d+)","type":"int"}]}]}`
	e2 := newEngine(t, noKV)
	r2 := e2.SyncProcessScreen(snap("Hello there"))
	if r2 == nil || len(r2.KVData) != 0 {
		t.Errorf("kv no-match = %+v", r2)
	}
}

func TestEngineProcessScreen(t *testing.T) {
	pinNow(t, 1000.0)
	e := newEngine(t, simpleRulesJSON)
	result := e.ProcessScreen(snap("Hello there"))
	if result == nil || result.PromptID != "prompt.hello" {
		t.Fatalf("result = %+v", result)
	}
	if result.IsIdle == nil {
		t.Error("is_idle populated")
	}
	if result.Buffer == nil {
		t.Error("buffer populated")
	}
	if result.Buffer.MatchedPromptID != "prompt.hello" {
		t.Error("buffer matched prompt id")
	}
}

func TestEngineHooks(t *testing.T) {
	pinNow(t, 1000.0)
	e := newEngine(t, simpleRulesJSON)
	var calls []*PromptDetection
	e.AddHook(func(s Snapshot, d *PromptDetection, b *ScreenBuffer, idle bool) error {
		if s == nil || b == nil {
			t.Error("hook args")
		}
		calls = append(calls, d)
		return nil
	})
	e.ProcessScreen(snap("Hello there"))
	if len(calls) != 1 || calls[0] == nil || calls[0].PromptID != "prompt.hello" {
		t.Errorf("calls = %v", calls)
	}
	// hooks run on no-match too, with nil detection
	result := e.ProcessScreen(snap("Unrecognized screen"))
	if result != nil {
		t.Error("no-match returns nil")
	}
	if len(calls) != 2 || calls[1] != nil {
		t.Error("hook called with nil detection")
	}
}

func TestEngineHookErrorsAreSwallowed(t *testing.T) {
	pinNow(t, 1000.0)
	e := newEngine(t, simpleRulesJSON)
	secondCalled := false
	e.AddHook(func(Snapshot, *PromptDetection, *ScreenBuffer, bool) error {
		return errors.New("boom")
	})
	e.AddHook(func(Snapshot, *PromptDetection, *ScreenBuffer, bool) error {
		secondCalled = true
		return nil
	})
	result := e.ProcessScreen(snap("Hello there"))
	if result == nil || !secondCalled {
		t.Error("bad hook must not stop later hooks or detection")
	}
	if e.HookCount() != 2 {
		t.Error("hook count")
	}
}

func TestEngineAddHookTwice(t *testing.T) {
	e := newEngine(t, simpleRulesJSON)
	hook := func(Snapshot, *PromptDetection, *ScreenBuffer, bool) error { return nil }
	e.AddHook(hook)
	e.AddHook(hook)
	if e.HookCount() != 2 {
		t.Error("no dedup contract")
	}
}

func TestEngineScreenSaverIntegration(t *testing.T) {
	pinNow(t, 1000.0)
	saver := NewScreenSaver(t.TempDir(), "", true)
	e := newEngine(t, simpleRulesJSON, WithScreenSaver(saver))
	e.ProcessScreen(snap("Hello there"))
	if saver.GetSavedCount() == 0 {
		t.Error("saver called on match")
	}
	// saver failure does not discard detection
	saver.writeFile = func(string, string) error { return errors.New("disk full") }
	result := e.ProcessScreen(snap("Hello there again"))
	if result == nil || result.PromptID != "prompt.hello" {
		t.Error("detection survives saver failure")
	}
}

func TestEngineNamespace(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "old", true)
	e := newEngine(t, simpleRulesJSON, WithScreenSaver(saver), WithNamespace("old"))
	e.SetNamespace("new_game")
	if e.Namespace() != "new_game" || saver.Namespace() != "new_game" {
		t.Error("namespace propagation")
	}
	// without saver — no panic
	e2 := newEngine(t, simpleRulesJSON, WithNamespace("old"))
	e2.SetNamespace("new_ns")
	if e2.Namespace() != "new_ns" {
		t.Error("namespace without saver")
	}
}

func TestEngineScreenSaverStatus(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "game1", true)
	e := newEngine(t, simpleRulesJSON, WithScreenSaver(saver))
	status := e.GetScreenSaverStatus()
	if status["enabled"] != true {
		t.Error("enabled")
	}
	if _, ok := status["saved_count"]; !ok {
		t.Error("saved_count")
	}
	if status["namespace"] != "game1" {
		t.Error("namespace")
	}
	e.SetScreenSaving(false)
	if saver.Enabled() {
		t.Error("SetScreenSaving false")
	}
	e.SetScreenSaving(true)
	if !saver.Enabled() {
		t.Error("SetScreenSaving true")
	}
	// no saver variants
	e2 := newEngine(t, simpleRulesJSON)
	if status2 := e2.GetScreenSaverStatus(); status2["enabled"] != false || len(status2) != 1 {
		t.Errorf("no-saver status = %v", status2)
	}
	e2.SetScreenSaving(true) // no-op, no panic
}

func TestEngineIsIdleAndDebugState(t *testing.T) {
	pinNow(t, 1000.0)
	e := newEngine(t, simpleRulesJSON)
	if e.IsIdle() {
		t.Error("fresh engine not idle")
	}
	state := e.DebugState()
	sb := state["screen_buffer"].(map[string]any)
	if sb["size"] != 0 || sb["is_idle"] != false || sb["last_change_seconds_ago"] != 0.0 {
		t.Errorf("empty debug state = %v", sb)
	}
	if state["screen_saver"] != nil {
		t.Error("no saver in debug state")
	}
	e.ProcessScreen(snap("Hello there"))
	state = e.DebugState()
	sb = state["screen_buffer"].(map[string]any)
	if sb["size"] != 1 || sb["max_size"] != 50 {
		t.Errorf("debug state after screen = %v", sb)
	}
	// with saver
	saver := NewScreenSaver(t.TempDir(), "", true)
	e2 := newEngine(t, simpleRulesJSON, WithScreenSaver(saver))
	if e2.DebugState()["screen_saver"] == nil {
		t.Error("saver in debug state")
	}
}

func TestEngineOptions(t *testing.T) {
	pinNow(t, 1000.0)
	e := newEngine(t, simpleRulesJSON, WithBufferSize(2), WithIdleThreshold(5.0))
	state := e.DebugState()
	if state["idle_threshold_s"] != 5.0 {
		t.Error("idle threshold option")
	}
	if state["screen_buffer"].(map[string]any)["max_size"] != 2 {
		t.Error("buffer size option")
	}
}

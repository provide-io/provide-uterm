//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// The Python detection module receives its normalizer as an injected callback
// (callers pass provide.uterm.screen.normalize_terminal_text). This test wires
// the Go equivalents together: screen.NormalizeTerminalText as the detector /
// engine normalizer.
func TestScreenNormalizerIntegration(t *testing.T) {
	d, err := NewPromptDetector(makePatterns(), WithNormalizer(screen.NormalizeTerminalText))
	if err != nil {
		t.Fatal(err)
	}
	// The same prompt with and without ANSI SGR noise fingerprints identically
	// once normalized.
	fp1 := d.PromptFingerprint(snap("Enter your name:"))
	fp2 := d.PromptFingerprint(snap("\x1b[1;31mEnter your name:\x1b[0m"))
	if fp1 != fp2 {
		t.Error("ANSI-noisy prompt should fingerprint identically after normalization")
	}

	e, err := NewDetectionEngine(simpleRulesJSON, WithEngineNormalizer(screen.NormalizeTerminalText))
	if err != nil {
		t.Fatal(err)
	}
	if e.SyncProcessScreen(snap("Hello there")) == nil {
		t.Error("engine with screen normalizer should detect")
	}
}

// End-to-end: rules JSON -> engine -> detection + KV, mirroring the Python
// test_integration end-to-end case.
func TestEndToEndDetectAndExtract(t *testing.T) {
	e, err := NewDetectionEngine(kvRulesJSON)
	if err != nil {
		t.Fatal(err)
	}
	result := e.SyncProcessScreen(snap("Sector 42 : Credits: 15,000\nCommand prompt"))
	if result == nil || result.PromptID != "prompt.sector" {
		t.Fatalf("result = %+v", result)
	}
	if result.KVData["sector"] != 42 || result.KVData["credits"] != 15000 {
		t.Errorf("kv = %v", result.KVData)
	}
}

// Reload mid-session, mirroring test_reload_mid_session.
func TestReloadMidSession(t *testing.T) {
	rulesA := `{"version":"1.0","game":"t","prompts":[{"id":"p.a","match":{"pattern":"AAA","match_mode":"contains"},"input_type":"single_key"}]}`
	rulesB := `{"version":"1.0","game":"t","prompts":[{"id":"p.b","match":{"pattern":"BBB","match_mode":"contains"},"input_type":"single_key"}]}`
	e, err := NewDetectionEngine(rulesA)
	if err != nil {
		t.Fatal(err)
	}
	if e.SyncProcessScreen(snap("AAA")) == nil || e.SyncProcessScreen(snap("BBB")) != nil {
		t.Error("rules A behavior")
	}
	if err := e.ReloadRules(rulesB); err != nil {
		t.Fatal(err)
	}
	if e.SyncProcessScreen(snap("AAA")) != nil || e.SyncProcessScreen(snap("BBB")) == nil {
		t.Error("rules B behavior after reload")
	}
}

// Multiple patterns: the first in rule order wins, mirroring
// test_multiple_patterns_first_match_wins (specific-before-generic ordering).
func TestMultiplePatternsFirstMatchWins(t *testing.T) {
	rules := `{"version":"1.0","game":"t","prompts":[
		{"id":"p.first","match":{"pattern":"Hello","match_mode":"contains"},"input_type":"single_key"},
		{"id":"p.second","match":{"pattern":"Hello there","match_mode":"contains"},"input_type":"multi_key"}]}`
	e, err := NewDetectionEngine(rules)
	if err != nil {
		t.Fatal(err)
	}
	result := e.SyncProcessScreen(snap("Hello there"))
	if result == nil || result.PromptID != "p.first" {
		t.Fatalf("result = %+v", result)
	}
}

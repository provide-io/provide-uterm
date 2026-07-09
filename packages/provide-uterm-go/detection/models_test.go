//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import "testing"

func TestPromptMatchModel(t *testing.T) {
	m := PromptMatch{PromptID: "p.a", Pattern: Pattern{"regex": "A"}, InputType: "single_key", EOLPattern: "$"}
	if m.PromptID != "p.a" {
		t.Error("prompt id")
	}
	if m.KVExtract != nil {
		t.Error("kv extract should default nil")
	}
}

func TestPromptMatchWithKVExtract(t *testing.T) {
	m := PromptMatch{
		PromptID:   "p.b",
		Pattern:    Pattern{},
		InputType:  "multi_key",
		EOLPattern: "$",
		KVExtract:  []any{map[string]any{"field": "credits", "regex": `(\d+)`, "type": "int"}},
	}
	kv, ok := m.KVExtract.([]any)
	if !ok || len(kv) != 1 {
		t.Error("kv extract length")
	}
}

func TestPromptDetectionDefaults(t *testing.T) {
	d := PromptDetection{PromptID: "p.login", InputType: "multi_key"}
	if d.KVData != nil {
		// zero value is nil map; treat as empty
		t.Log("kv nil ok")
	}
	if d.Match != nil {
		t.Error("match default nil")
	}
	if d.IsIdle != nil {
		t.Error("is_idle default nil")
	}
	if d.Buffer != nil {
		t.Error("buffer default nil")
	}
}

func TestPromptDetectionWithMatch(t *testing.T) {
	m := &PromptMatch{PromptID: "p.a", Pattern: Pattern{}, InputType: "single_key", EOLPattern: "$"}
	d := PromptDetection{PromptID: "p.a", InputType: "single_key", KVData: map[string]any{"x": 1}, Match: m}
	if d.Match.PromptID != "p.a" {
		t.Error("match prompt id")
	}
	if d.KVData["x"] != 1 {
		t.Error("kv data")
	}
}

func TestDiagnosticsEmpty(t *testing.T) {
	diag := PromptDetectionDiagnostics{}
	if diag.Match != nil {
		t.Error("match nil")
	}
	if len(diag.RegexMatchedButFailed) != 0 {
		t.Error("failed empty")
	}
}

func TestDiagnosticsWithFailures(t *testing.T) {
	diag := PromptDetectionDiagnostics{
		RegexMatchedButFailed: []map[string]any{{"id": "p.x", "reason": "cursor_miss"}},
	}
	if len(diag.RegexMatchedButFailed) != 1 {
		t.Error("failed length")
	}
}

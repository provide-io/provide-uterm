//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"encoding/json"
	"os"
	"testing"
)

// Differential test: re-run the Python-built corpus through the Go port and
// assert byte-identical decisions. The golden is produced by
// scratchpad/dump_differential.py and committed under testdata/.

type goldenFile struct {
	DetectorCases  []json.RawMessage `json:"detector_cases"`
	FlowCases      []json.RawMessage `json:"flow_cases"`
	IdleCases      []json.RawMessage `json:"idle_cases"`
	InputTypeCases []json.RawMessage `json:"input_type_cases"`
}

func loadGolden(t *testing.T) goldenFile {
	t.Helper()
	data, err := os.ReadFile("testdata/differential_golden.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g goldenFile
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	return g
}

func canonical(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

// canonicalExpected re-marshals a decoded expected value so numeric types are
// normalized identically to the Go-computed result.
func canonicalExpected(t *testing.T, raw map[string]any) string {
	t.Helper()
	return canonical(t, raw)
}

func TestDifferentialDetectorCases(t *testing.T) {
	g := loadGolden(t)
	if len(g.DetectorCases) == 0 {
		t.Fatal("no detector cases in golden")
	}
	for _, raw := range g.DetectorCases {
		var c struct {
			Name     string           `json:"name"`
			Patterns []map[string]any `json:"patterns"`
			Rules    json.RawMessage  `json:"rules"`
			Snapshot map[string]any   `json:"snapshot"`
			Expected map[string]any   `json:"expected"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("case decode: %v", err)
		}
		t.Run(c.Name, func(t *testing.T) {
			patterns := c.Patterns
			if len(c.Rules) > 0 {
				rs, err := RuleSetFromJSON(c.Rules)
				if err != nil {
					t.Fatalf("rules: %v", err)
				}
				patterns = rs.ToPromptPatterns()
			}
			det := mustDetector(patterns)
			match := det.DetectPrompt(c.Snapshot)
			var result map[string]any
			if match == nil {
				result = map[string]any{"matched": false}
			} else {
				kvData := map[string]any{}
				if pyTruthy(match.KVExtract) {
					screen, _ := c.Snapshot["screen"].(string)
					if extracted := ExtractKV(screen, match.KVExtract); extracted != nil {
						kvData = extracted
					}
				}
				result = map[string]any{
					"matched":    true,
					"prompt_id":  match.PromptID,
					"input_type": match.InputType,
					"kv_data":    kvData,
				}
			}
			got := canonical(t, result)
			want := canonicalExpected(t, c.Expected)
			if got != want {
				t.Errorf("mismatch\n got: %s\nwant: %s", got, want)
			}
		})
	}
}

func TestDifferentialFlowCases(t *testing.T) {
	g := loadGolden(t)
	if len(g.FlowCases) == 0 {
		t.Fatal("no flow cases in golden")
	}
	for _, raw := range g.FlowCases {
		var c struct {
			Name     string          `json:"name"`
			Rules    json.RawMessage `json:"rules"`
			Flow     string          `json:"flow"`
			Screen   string          `json:"screen"`
			Cursor   []int           `json:"cursor"`
			Expected map[string]any  `json:"expected"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("case decode: %v", err)
		}
		t.Run(c.Name, func(t *testing.T) {
			rs, err := RuleSetFromJSON(c.Rules)
			if err != nil {
				t.Fatalf("rules: %v", err)
			}
			fe := NewFlowEngine(rs)
			var cursor *[2]int
			if len(c.Cursor) == 2 {
				cursor = &[2]int{c.Cursor[0], c.Cursor[1]}
			}
			step, err := fe.Advance(c.Flow, c.Screen, cursor)
			if err != nil {
				t.Fatalf("advance: %v", err)
			}
			result := map[string]any{
				"current_prompt_id": ptrToAny(step.CurrentPromptID),
				"next_action":       ptrToAny(step.NextAction),
				"done":              step.Done,
				"kv_data":           step.KVData,
			}
			got := canonical(t, result)
			want := canonicalExpected(t, c.Expected)
			if got != want {
				t.Errorf("mismatch\n got: %s\nwant: %s", got, want)
			}
		})
	}
}

func TestDifferentialIdleCases(t *testing.T) {
	g := loadGolden(t)
	if len(g.IdleCases) == 0 {
		t.Fatal("no idle cases in golden")
	}
	for _, raw := range g.IdleCases {
		var c struct {
			Name      string           `json:"name"`
			Screens   []map[string]any `json:"screens"`
			Now       float64          `json:"now"`
			Threshold float64          `json:"threshold"`
			Expected  map[string]any   `json:"expected"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("case decode: %v", err)
		}
		t.Run(c.Name, func(t *testing.T) {
			restore := nowSeconds
			nowSeconds = func() float64 { return c.Now }
			defer func() { nowSeconds = restore }()

			mgr := NewBufferManager(50)
			for _, s := range c.Screens {
				mgr.AddScreen(s)
			}
			result := map[string]any{"is_idle": mgr.DetectIdleState(c.Threshold)}
			got := canonical(t, result)
			want := canonicalExpected(t, c.Expected)
			if got != want {
				t.Errorf("mismatch\n got: %s\nwant: %s", got, want)
			}
		})
	}
}

func TestDifferentialInputTypeCases(t *testing.T) {
	g := loadGolden(t)
	if len(g.InputTypeCases) == 0 {
		t.Fatal("no input-type cases in golden")
	}
	for _, raw := range g.InputTypeCases {
		var c struct {
			Screen   string `json:"screen"`
			Expected string `json:"expected"`
		}
		if err := json.Unmarshal(raw, &c); err != nil {
			t.Fatalf("case decode: %v", err)
		}
		if got := AutoDetectInputType(c.Screen); got != c.Expected {
			t.Errorf("AutoDetectInputType(%q) = %q, want %q", c.Screen, got, c.Expected)
		}
	}
}

func ptrToAny(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

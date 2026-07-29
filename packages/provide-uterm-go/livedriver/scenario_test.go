//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestParseScenarioMinimal(t *testing.T) {
	sc, err := ParseScenario([]byte(`{
		"id": "010_health", "title": "Health",
		"steps": [{"id": "health", "action": "health"}],
		"expect": [{"step": "health", "path": "body.status", "equals": "ok"}]
	}`))
	if err != nil {
		t.Fatalf("ParseScenario: %v", err)
	}
	if sc.ID != "010_health" || sc.Title != "Health" || len(sc.Steps) != 1 {
		t.Fatalf("parsed wrong: %+v", sc)
	}
	if sc.Steps[0].Action != ActionHealth || sc.Steps[0].Auth != "" {
		t.Fatalf("step wrong: %+v", sc.Steps[0])
	}
}

func TestParseScenarioAllStepFields(t *testing.T) {
	sc, err := ParseScenario([]byte(`{
		"id": "020_post", "title": "Post", "timeout_ms": 500,
		"requires": ["hijack.rest"], "auth": "jwt",
		"steps": [{"id": "s", "action": "http_post", "auth": "bad",
		           "path": "/api/x", "session_id": "sess", "body": {"a": 1},
		           "volatile": ["body.ts"]}]
	}`))
	if err != nil {
		t.Fatalf("ParseScenario: %v", err)
	}
	st := sc.Steps[0]
	if st.Auth != AuthBad || st.Path != "/api/x" || st.SessionID != "sess" {
		t.Fatalf("step wrong: %+v", st)
	}
	if string(st.Body) != `{"a": 1}` {
		t.Fatalf("body not kept verbatim: %s", st.Body)
	}
	if !reflect.DeepEqual(sc.Requires, []string{"hijack.rest"}) {
		t.Fatalf("requires = %v", sc.Requires)
	}
}

func TestParseScenarioRejects(t *testing.T) {
	cases := []struct{ name, data, want string }{
		{"not json", `{`, "not valid JSON"},
		{"no id", `{"steps": [{"id": "a", "action": "health"}]}`, "no id"},
		{"blank id", `{"id": "  ", "steps": [{"id": "a", "action": "health"}]}`, "no id"},
		{"no steps", `{"id": "x"}`, "no steps"},
		{"empty steps", `{"id": "x", "steps": []}`, "no steps"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseScenario([]byte(tc.data))
			if err == nil {
				t.Fatal("expected an error")
			}
			if !contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err, tc.want)
			}
		})
	}
}

func TestScenarioTimeout(t *testing.T) {
	if got := (&Scenario{}).Timeout(); got != DefaultTimeoutMS*time.Millisecond {
		t.Fatalf("default timeout = %v", got)
	}
	if got := (&Scenario{TimeoutMS: -5}).Timeout(); got != DefaultTimeoutMS*time.Millisecond {
		t.Fatalf("negative timeout = %v", got)
	}
	if got := (&Scenario{TimeoutMS: 250}).Timeout(); got != 250*time.Millisecond {
		t.Fatalf("explicit timeout = %v", got)
	}
}

func TestScenarioAuthMode(t *testing.T) {
	if got := (&Scenario{}).AuthMode(); got != "dev_token" {
		t.Fatalf("default auth mode = %q", got)
	}
	if got := (&Scenario{Auth: " jwt "}).AuthMode(); got != "jwt" {
		t.Fatalf("explicit auth mode = %q", got)
	}
}

func TestLoadScenario(t *testing.T) {
	path := filepath.Join(t.TempDir(), "030_x.json")
	body := `{"id": "030_x", "title": "x", "steps": [{"id": "a", "action": "health"}]}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	sc, err := LoadScenario(path)
	if err != nil || sc.ID != "030_x" {
		t.Fatalf("LoadScenario: %+v %v", sc, err)
	}
	if _, err := LoadScenario(filepath.Join(t.TempDir(), "nope.json")); err == nil {
		t.Fatal("expected a read error for a missing file")
	}
}

func TestScenarioIDFromPath(t *testing.T) {
	cases := map[string]string{
		"/a/b/010_health.json":     "010_health",
		"010_health.json":          "010_health",
		"plain":                    "plain",
		".":                        "",
		string(filepath.Separator): "",
	}
	for in, want := range cases {
		if got := ScenarioIDFromPath(in); got != want {
			t.Fatalf("ScenarioIDFromPath(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestMissingCapabilities(t *testing.T) {
	have := []string{"a", "b"}
	if got := MissingCapabilities(nil, have); got != nil {
		t.Fatalf("no requirements should be satisfied, got %v", got)
	}
	if got := MissingCapabilities([]string{"a", "b"}, have); got != nil {
		t.Fatalf("all present, got %v", got)
	}
	got := MissingCapabilities([]string{"z", "a", "y"}, have)
	if !reflect.DeepEqual(got, []string{"z", "y"}) {
		t.Fatalf("missing = %v, want [z y] in requirement order", got)
	}
}

// contains is a tiny substring helper so table tests read as assertions.
func contains(haystack, needle string) bool {
	return len(needle) == 0 || len(haystack) >= len(needle) &&
		func() bool {
			for i := 0; i+len(needle) <= len(haystack); i++ {
				if haystack[i:i+len(needle)] == needle {
					return true
				}
			}
			return false
		}()
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"context"
	"net/http"
	"strings"
	"testing"
)

// intOf is the pointer form a step uses for an integer it actually asked for,
// as against one it left out.
func intOf(v int) *int { return &v }

// lastRequest is what the fake server saw most recently.
func lastRequest(t *testing.T, fs *fakeServer) recordedRequest {
	t.Helper()
	seen := fs.requests()
	if len(seen) == 0 {
		t.Fatal("the server saw no request at all")
	}
	return seen[len(seen)-1]
}

// runSteps runs a whole scenario, so a test can watch one step use another's
// answer.
func runSteps(t *testing.T, baseURL string, steps ...Step) Result {
	t.Helper()
	sc := &Scenario{ID: "010_t", Steps: steps, TimeoutMS: 5000}
	return RunScenario(context.Background(), sc, ClientOptions{BaseURL: baseURL, Token: "tok"})
}

func TestWaveTwoActionsHitTheirEndpoints(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"ok":true}`))
	cases := []struct {
		name       string
		step       Step
		wantMethod string
		wantPath   string
		wantQuery  string
		wantBody   string
	}{
		{
			"session_events", Step{ID: "a", Action: ActionSessionEvents, SessionID: "s1", Limit: intOf(7)},
			http.MethodGet, "/api/sessions/s1/events", "limit=7", "",
		},
		{
			"session_events default limit", Step{ID: "a", Action: ActionSessionEvents, SessionID: "s1"},
			http.MethodGet, "/api/sessions/s1/events", "limit=100", "",
		},
		{
			"set_input_mode", Step{ID: "b", Action: ActionSetInputMode, SessionID: "s1", InputMode: "hijack"},
			http.MethodPost, "/api/sessions/s1/mode", "", `{"input_mode":"hijack"}`,
		},
		{
			"hijack_acquire defaults", Step{ID: "c", Action: ActionHijackAcquire, WorkerID: "w1"},
			http.MethodPost, "/worker/w1/hijack/acquire", "", `{"lease_s":90,"owner":"operator"}`,
		},
		{
			"hijack_acquire explicit",
			Step{ID: "c", Action: ActionHijackAcquire, WorkerID: "w1", Owner: "tester", LeaseS: intOf(5)},
			http.MethodPost, "/worker/w1/hijack/acquire", "", `{"lease_s":5,"owner":"tester"}`,
		},
		{
			"hijack_heartbeat", Step{ID: "d", Action: ActionHijackHeartbeat, WorkerID: "w1", HijackID: "h1"},
			http.MethodPost, "/worker/w1/hijack/h1/heartbeat", "", `{"lease_s":90}`,
		},
		{
			"hijack_send",
			Step{ID: "e", Action: ActionHijackSend, WorkerID: "w1", HijackID: "h1", Keys: "echo hi\n"},
			http.MethodPost, "/worker/w1/hijack/h1/send", "",
			`{"keys":"echo hi\n","poll_interval_ms":120,"timeout_ms":2000}`,
		},
		{
			"hijack_step", Step{ID: "f", Action: ActionHijackStep, WorkerID: "w1", HijackID: "h1"},
			http.MethodPost, "/worker/w1/hijack/h1/step", "", "",
		},
		{
			"hijack_snapshot", Step{ID: "g", Action: ActionHijackSnapshot, WorkerID: "w1", HijackID: "h1"},
			http.MethodGet, "/worker/w1/hijack/h1/snapshot", "wait_ms=1500", "",
		},
		{
			"hijack_release", Step{ID: "h", Action: ActionHijackRelease, WorkerID: "w1", HijackID: "h1"},
			http.MethodPost, "/worker/w1/hijack/h1/release", "", "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := runOneStep(t, fs.URL, "tok", tc.step)
			if r.Status != StatusCompleted {
				t.Fatalf("status = %s (%v)", r.Status, r.Error)
			}
			if f := r.Steps[0].Fields; f.Status == nil || *f.Status != 200 || !f.OK {
				t.Fatalf("fields = %+v", f)
			}
			got := lastRequest(t, fs)
			if got.method != tc.wantMethod || got.path != tc.wantPath {
				t.Fatalf("request = %s %s, want %s %s", got.method, got.path, tc.wantMethod, tc.wantPath)
			}
			if got.query != tc.wantQuery {
				t.Fatalf("query = %q, want %q", got.query, tc.wantQuery)
			}
			if tc.wantBody != "" && got.body != tc.wantBody {
				t.Fatalf("body = %s, want %s", got.body, tc.wantBody)
			}
		})
	}
}

func TestWaveTwoActionsRecordTheStatusUnderneathTheLibrary(t *testing.T) {
	// The refusal a second hijacker gets is a 409 with a body, and it has to
	// stay a 409 in the result rather than collapsing into ok:false.
	fs := newFakeServer(t, jsonHandler(409, `{"error":"Worker is already hijacked."}`))
	r := runOneStep(t, fs.URL, "tok", Step{ID: "a", Action: ActionHijackAcquire, WorkerID: "w1"})
	if r.Status != StatusCompleted {
		t.Fatalf("a 409 is an observation, not a run failure: %s", r.Status)
	}
	f := r.Steps[0].Fields
	if f.Status == nil || *f.Status != 409 || f.OK || f.Error != nil {
		t.Fatalf("fields = %+v", f)
	}
	body, ok := f.Body.(map[string]any)
	if !ok || body["error"] != "Worker is already hijacked." {
		t.Fatalf("body = %#v", f.Body)
	}
}

func TestWaveTwoActionsNeedTheirArguments(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{}`))
	cases := []struct {
		name string
		step Step
		want string
	}{
		{"events without session", Step{ID: "x", Action: ActionSessionEvents}, "requires session_id"},
		{"mode without session", Step{ID: "x", Action: ActionSetInputMode, InputMode: "open"}, "requires session_id"},
		{"mode without a mode", Step{ID: "x", Action: ActionSetInputMode, SessionID: "s1"}, "requires input_mode"},
		{"acquire without worker", Step{ID: "x", Action: ActionHijackAcquire}, "requires worker_id"},
		{"heartbeat without worker", Step{ID: "x", Action: ActionHijackHeartbeat}, "requires worker_id"},
		{
			"heartbeat without lease",
			Step{ID: "x", Action: ActionHijackHeartbeat, WorkerID: "w1"}, "requires hijack_id",
		},
		{"send without lease", Step{ID: "x", Action: ActionHijackSend, WorkerID: "w1"}, "requires hijack_id"},
		{"step without lease", Step{ID: "x", Action: ActionHijackStep, WorkerID: "w1"}, "requires hijack_id"},
		{"snapshot without lease", Step{ID: "x", Action: ActionHijackSnapshot, WorkerID: "w1"}, "requires hijack_id"},
		{"release without lease", Step{ID: "x", Action: ActionHijackRelease, WorkerID: "w1"}, "requires hijack_id"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := runOneStep(t, fs.URL, "tok", tc.step)
			if r.Status != StatusError {
				t.Fatalf("status = %s, want error", r.Status)
			}
			if r.Error == nil || !strings.Contains(*r.Error, tc.want) {
				t.Fatalf("error = %v, want it to mention %q", r.Error, tc.want)
			}
			if len(r.Steps) != 1 || r.Steps[0].Fields.Error == nil {
				t.Fatalf("the step the driver could not perform must still be reported: %+v", r.Steps)
			}
		})
	}
}

func TestStepDefaults(t *testing.T) {
	bare := Step{}
	if bare.owner() != DefaultOwner || bare.leaseS() != DefaultLeaseS || bare.limit() != DefaultEventsLimit {
		t.Fatalf("defaults = %q %d %d", bare.owner(), bare.leaseS(), bare.limit())
	}
	// A step that asked for zero asked for zero; only an absent field defaults.
	asked := Step{Owner: "someone", LeaseS: intOf(1), Limit: intOf(0)}
	if asked.owner() != "someone" || asked.leaseS() != 1 || asked.limit() != 0 {
		t.Fatalf("explicit = %q %d %d", asked.owner(), asked.leaseS(), asked.limit())
	}
}

func TestParseScenarioReadsWaveTwoStepFields(t *testing.T) {
	sc, err := ParseScenario([]byte(`{
		"id": "050_hijack", "title": "Hijack",
		"steps": [{"id": "acq", "action": "hijack_acquire", "worker_id": "w1",
		           "hijack_id": "h1", "owner": "tester", "lease_s": 30,
		           "keys": "ls\n", "input_mode": "hijack", "limit": 5}]
	}`))
	if err != nil {
		t.Fatalf("ParseScenario: %v", err)
	}
	st := sc.Steps[0]
	if st.WorkerID != "w1" || st.HijackID != "h1" || st.Owner != "tester" {
		t.Fatalf("step wrong: %+v", st)
	}
	if st.leaseS() != 30 || st.limit() != 5 || st.Keys != "ls\n" || st.InputMode != "hijack" {
		t.Fatalf("step wrong: %+v", st)
	}
}

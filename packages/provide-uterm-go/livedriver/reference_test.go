//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

// recorded is what one earlier step saw, built the way a real run builds it so
// the resolver is walking the same shapes (json.Number and all).
func recorded(id string, status int, raw string) map[string]StepFields {
	return map[string]StepFields{id: rawFields(status, []byte(raw))}
}

// TestReferenceResolvesAnEarlierAnswer is the guard the resolver exists for.
//
// It asserts the *value arrived*, not that the code path ran: a resolver whose
// pattern is subtly wrong (doubled backslashes matching a literal backslash, an
// anchor in the wrong place) still runs, still returns no error, and quietly
// sends "${acq.body.hijack_id}" as the lease id. Only the request the server
// actually received can tell the two apart.
func TestReferenceResolvesAnEarlierAnswer(t *testing.T) {
	fs := newFakeServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "/hijack/acquire") {
			_, _ = w.Write([]byte(`{"hijack_id":"HJ-7"}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	})

	r := runSteps(t, fs.URL,
		Step{ID: "acq", Action: ActionHijackAcquire, WorkerID: "w1"},
		Step{ID: "snap", Action: ActionHijackSnapshot, WorkerID: "w1", HijackID: "${acq.body.hijack_id}"},
	)
	if r.Status != StatusCompleted {
		t.Fatalf("status = %s (%v)", r.Status, r.Error)
	}
	got := lastRequest(t, fs)
	if got.path != "/worker/w1/hijack/HJ-7/snapshot" {
		t.Fatalf("the reference did not resolve: request went to %q", got.path)
	}
}

func TestResolveTextReadsWhatAStepRecorded(t *testing.T) {
	seen := recorded("a", 201, `{"id":"abc","items":[{"id":"first"},{"id":"second"}],"n":3,"nothing":null}`)
	cases := map[string]string{
		"${a.body.id}":         "abc",
		"${a.body.items.1.id}": "second",
		"${a.body.n}":          "3",
		"${a.body.nothing}":    "null",
		"${a.status}":          "201",
		"${a.ok}":              "true",
		"${a.error}":           "null",
		"${a.body}":            `{"id":"abc","items":[{"id":"first"},{"id":"second"}],"n":3,"nothing":null}`,
	}
	for ref, want := range cases {
		got, err := resolveText(ref, seen)
		if err != nil {
			t.Fatalf("resolveText(%s): %v", ref, err)
		}
		if got != want {
			t.Fatalf("resolveText(%s) = %q, want %q", ref, got, want)
		}
	}
}

func TestOnlyAWholeFieldIsAReference(t *testing.T) {
	seen := recorded("a", 200, `{"id":"abc"}`)
	// Every one of these is sent as written: the grammar has no interpolation,
	// no nesting, and no expressions, so anything but an exact reference is a
	// literal — including one that merely looks like it should work.
	for _, literal := range []string{
		"a${a.body.id}b",
		"${a.body.id}b",
		"a${a.body.id}",
		"${a.body.id} ",
		"${a.body.id}${a.body.id}",
		"$ {a.body.id}",
		"{a.body.id}",
		"${a}",
		"${a-b.id}",
		"${A.body.id}",
		"${a.body id}",
		"",
		"plain",
	} {
		got, err := resolveText(literal, seen)
		if err != nil {
			t.Fatalf("resolveText(%q): %v", literal, err)
		}
		if got != literal {
			t.Fatalf("resolveText(%q) = %q, want it sent as written", literal, got)
		}
	}
}

func TestAReferenceThatIsNotThereIsAnError(t *testing.T) {
	seen := recorded("a", 200, `{"id":"abc","items":[{"id":"first"}]}`)
	cases := map[string]string{
		"${nope.body.id}":       `names step "nope", which has not run`,
		"${a.nosuchfield}":      "is not there",
		"${a.body.missing}":     "is not there",
		"${a.body.items.9}":     "is not there",
		"${a.body.items.first}": "is not there",
		"${a.body.id.deeper}":   "is not there",
		"${a.body..id}":         "is not there",
	}
	for ref, want := range cases {
		_, err := resolveText(ref, seen)
		if err == nil {
			t.Fatalf("resolveText(%s) should be a run error", ref)
		}
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("resolveText(%s) error = %q, want it to mention %q", ref, err, want)
		}
	}
}

func TestResolveStepCoversEveryReferenceableField(t *testing.T) {
	seen := map[string]StepFields{
		"a": rawFields(200, []byte(`{"auth":"none","path":"/p","session":"s","worker":"w",
		                             "lease":"h","owner":"o","keys":"k","mode":"open"}`)),
	}
	step := Step{
		ID: "b", Action: ActionHijackSend,
		Auth:      "${a.body.auth}",
		Path:      "${a.body.path}",
		SessionID: "${a.body.session}",
		WorkerID:  "${a.body.worker}",
		HijackID:  "${a.body.lease}",
		Owner:     "${a.body.owner}",
		Keys:      "${a.body.keys}",
		InputMode: "${a.body.mode}",
	}
	if err := resolveStep(&step, seen); err != nil {
		t.Fatalf("resolveStep: %v", err)
	}
	want := Step{
		ID: "b", Action: ActionHijackSend,
		Auth: "none", Path: "/p", SessionID: "s", WorkerID: "w",
		HijackID: "h", Owner: "o", Keys: "k", InputMode: "open",
	}
	if !reflect.DeepEqual(step, want) {
		t.Fatalf("resolveStep = %+v, want %+v", step, want)
	}
}

func TestResolveStepStopsAtTheFirstBadReference(t *testing.T) {
	step := Step{ID: "b", Action: ActionHijackSend, WorkerID: "${gone.body.id}"}
	if err := resolveStep(&step, map[string]StepFields{}); err == nil {
		t.Fatal("expected a run error")
	}
}

func TestResolveBody(t *testing.T) {
	seen := recorded("a", 200, `{"nested":{"k":1}}`)

	// A body written as a reference is substituted as JSON, so a step can post
	// back the object an earlier step was given rather than its text.
	step := Step{ID: "b", Action: ActionHTTPPost, Body: json.RawMessage(`"${a.body.nested}"`)}
	if err := resolveStep(&step, seen); err != nil {
		t.Fatalf("resolveStep: %v", err)
	}
	if string(step.Body) != `{"k":1}` {
		t.Fatalf("body = %s, want the referenced object", step.Body)
	}

	// Everything else is left byte-for-byte alone.
	for _, body := range []string{`{"a": 1}`, `"plain string"`, `"a${a.body.nested}b"`, ``} {
		untouched := Step{ID: "b", Action: ActionHTTPPost, Body: json.RawMessage(body)}
		if err := resolveStep(&untouched, seen); err != nil {
			t.Fatalf("resolveStep(%s): %v", body, err)
		}
		if string(untouched.Body) != body {
			t.Fatalf("body %s became %s", body, untouched.Body)
		}
	}

	// A body reference that is not there is a run error like any other.
	bad := Step{ID: "b", Action: ActionHTTPPost, Body: json.RawMessage(`"${a.body.missing}"`)}
	if err := resolveStep(&bad, seen); err == nil {
		t.Fatal("expected a run error")
	}
}

func TestAnUnresolvableReferenceIsARunErrorNotAnObservation(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"hijack_id":"HJ-7"}`))
	r := runSteps(t, fs.URL,
		Step{ID: "acq", Action: ActionHijackAcquire, WorkerID: "w1"},
		Step{ID: "snap", Action: ActionHijackSnapshot, WorkerID: "w1", HijackID: "${acq.body.nope}"},
		Step{ID: "never", Action: ActionHealth},
	)
	if r.Status != StatusError {
		t.Fatalf("status = %s, want error", r.Status)
	}
	if r.Error == nil || !strings.Contains(*r.Error, "step snap:") {
		t.Fatalf("error should name the step: %v", r.Error)
	}
	// Only the step that ran is reported. Recording the malformed one as a
	// field would let the harness compare it as though the server had answered.
	if len(r.Steps) != 1 || r.Steps[0].ID != "acq" {
		t.Fatalf("steps = %+v, want only the step that actually ran", r.Steps)
	}
}

func TestReferencesIntoAStepThatGotNoResponse(t *testing.T) {
	// A step that never reached the server records a null status and an error
	// message. Both are answers, and both are readable — absent is what a
	// malformed reference is, and this is not that.
	seen := map[string]StepFields{"a": failFields("dial tcp: refused")}
	for ref, want := range map[string]string{
		"${a.status}": "null",
		"${a.error}":  "dial tcp: refused",
		"${a.ok}":     "false",
	} {
		got, err := resolveText(ref, seen)
		if err != nil {
			t.Fatalf("resolveText(%s): %v", ref, err)
		}
		if got != want {
			t.Fatalf("resolveText(%s) = %q, want %q", ref, got, want)
		}
	}
}

func TestABodyReferenceJSONCannotHoldIsARunError(t *testing.T) {
	// Unreachable from a real response, but the guard is what keeps a resolver
	// bug from putting a half-encoded body on the wire.
	seen := map[string]StepFields{"a": {Body: func() {}}}
	step := Step{ID: "b", Action: ActionHTTPPost, Body: json.RawMessage(`"${a.body}"`)}
	err := resolveStep(&step, seen)
	if err == nil || !strings.Contains(err.Error(), "cannot be sent as a body") {
		t.Fatalf("error = %v, want it to refuse the body", err)
	}
}

func TestAsTextFallsBackForAValueJSONCannotHold(t *testing.T) {
	// Unreachable from a real response — every recorded value came out of one —
	// but the fallback is what keeps a resolver bug from becoming a panic.
	if got := asText(func() {}); got == "" {
		t.Fatal("asText should still render a value json.Marshal refuses")
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// recordedRequest is what the fake server saw.
type recordedRequest struct {
	method string
	path   string
	auth   string
	body   string
}

// fakeServer answers the routes the six actions hit and remembers every
// request, so a test can assert both what was sent and what was recorded.
type fakeServer struct {
	*httptest.Server
	mu   sync.Mutex
	seen []recordedRequest
}

func newFakeServer(t *testing.T, handler http.HandlerFunc) *fakeServer {
	t.Helper()
	fs := &fakeServer{}
	fs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		fs.mu.Lock()
		fs.seen = append(fs.seen, recordedRequest{
			method: r.Method, path: r.URL.Path,
			auth: r.Header.Get("Authorization"), body: string(body),
		})
		fs.mu.Unlock()
		handler(w, r)
	}))
	t.Cleanup(fs.Close)
	return fs
}

func (fs *fakeServer) requests() []recordedRequest {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	return append([]recordedRequest(nil), fs.seen...)
}

// jsonHandler answers every path with a fixed status and body.
func jsonHandler(status int, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func runOneStep(t *testing.T, baseURL, token string, step Step) Result {
	t.Helper()
	sc := &Scenario{ID: "010_t", Steps: []Step{step}, TimeoutMS: 5000}
	return RunScenario(context.Background(), sc, ClientOptions{BaseURL: baseURL, Token: token})
}

func TestAuthHeaders(t *testing.T) {
	withToken := newStepRunner(ClientOptions{Token: "tok"})
	cases := []struct {
		name, mode, want string
	}{
		{"default", "", "Bearer tok"},
		{"explicit token", AuthToken, "Bearer tok"},
		{"none", AuthNone, ""},
		{"bad", AuthBad, "Bearer " + BadToken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h, err := withToken.authHeaders(tc.mode)
			if err != nil {
				t.Fatalf("authHeaders: %v", err)
			}
			if h["Authorization"] != tc.want {
				t.Fatalf("Authorization = %q, want %q", h["Authorization"], tc.want)
			}
		})
	}

	// An empty token means there is nothing to present; "Bearer " would be a
	// third, unspecified credential shape.
	noToken := newStepRunner(ClientOptions{})
	if h, err := noToken.authHeaders(AuthToken); err != nil || len(h) != 0 {
		t.Fatalf("empty token → %v %v", h, err)
	}
	if _, err := withToken.authHeaders("sideways"); err == nil {
		t.Fatal("an unknown auth mode must be a driver error, not a guess")
	}
}

func TestAuthHeadersReachTheServer(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"status":"ok"}`))
	for _, mode := range []string{AuthToken, AuthNone, AuthBad} {
		runOneStep(t, fs.URL, "real-token", Step{ID: "h", Action: ActionHealth, Auth: mode})
	}
	got := fs.requests()
	want := []string{"Bearer real-token", "", "Bearer " + BadToken}
	for i, w := range want {
		if got[i].auth != w {
			t.Fatalf("request %d Authorization = %q, want %q", i, got[i].auth, w)
		}
	}
}

func TestLibraryActionsHitTheirEndpoints(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"status":"ok"}`))
	cases := []struct {
		step       Step
		wantMethod string
		wantPath   string
	}{
		{Step{ID: "a", Action: ActionHealth}, http.MethodGet, "/api/health"},
		{Step{ID: "b", Action: ActionListSessions}, http.MethodGet, "/api/sessions"},
		{Step{ID: "c", Action: ActionGetSession, SessionID: "s1"}, http.MethodGet, "/api/sessions/s1"},
		{Step{ID: "d", Action: ActionSessionSnapshot, SessionID: "s1"}, http.MethodGet, "/api/sessions/s1/snapshot"},
	}
	for _, tc := range cases {
		t.Run(tc.step.Action, func(t *testing.T) {
			r := runOneStep(t, fs.URL, "tok", tc.step)
			if r.Status != StatusCompleted {
				t.Fatalf("status = %s (%v)", r.Status, r.Error)
			}
			f := r.Steps[0].Fields
			if f.Status == nil || *f.Status != 200 || !f.OK {
				t.Fatalf("fields = %+v", f)
			}
			last := fs.requests()[len(fs.requests())-1]
			if last.method != tc.wantMethod || last.path != tc.wantPath {
				t.Fatalf("request = %s %s, want %s %s", last.method, last.path, tc.wantMethod, tc.wantPath)
			}
		})
	}
}

func TestRawActions(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(201, `{"created":true}`))

	r := runOneStep(t, fs.URL, "tok", Step{ID: "g", Action: ActionHTTPGet, Path: "/api/anything"})
	if f := r.Steps[0].Fields; *f.Status != 201 || !f.OK {
		t.Fatalf("http_get fields = %+v", f)
	}

	r = runOneStep(t, fs.URL, "tok", Step{
		ID: "p", Action: ActionHTTPPost, Path: "/api/connect",
		Body: json.RawMessage(`{"connector_type":"shell"}`),
	})
	if f := r.Steps[0].Fields; *f.Status != 201 || !f.OK {
		t.Fatalf("http_post fields = %+v", f)
	}
	last := fs.requests()[len(fs.requests())-1]
	if last.method != http.MethodPost || last.path != "/api/connect" {
		t.Fatalf("post went to %s %s", last.method, last.path)
	}
	if last.body != `{"connector_type":"shell"}` {
		t.Fatalf("body was not sent verbatim: %s", last.body)
	}
}

func TestStatusIsObservedUnderneathTheLibrary(t *testing.T) {
	// The whole point of the recording transport: three refusals the library
	// reports identically must stay distinguishable in the result.
	for _, status := range []int{401, 403, 404, 409, 500} {
		fs := newFakeServer(t, jsonHandler(status, `{"error":"nope"}`))
		r := runOneStep(t, fs.URL, "tok", Step{ID: "h", Action: ActionHealth})
		if r.Status != StatusCompleted {
			t.Fatalf("a %d is an observation, not a run failure: %s", status, r.Status)
		}
		f := r.Steps[0].Fields
		if f.Status == nil || *f.Status != status {
			t.Fatalf("status = %v, want %d", f.Status, status)
		}
		if f.OK {
			t.Fatalf("%d reported ok", status)
		}
		if f.Error != nil {
			t.Fatalf("%d set error=%q; a refusal is an observation", status, *f.Error)
		}
		body, ok := f.Body.(map[string]any)
		if !ok || body["error"] != "nope" {
			t.Fatalf("%d body = %#v", status, f.Body)
		}
	}
}

func TestNonJSONBodyCollapses(t *testing.T) {
	fs := newFakeServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, "<!doctype html><p>hi")
	})
	for _, step := range []Step{
		{ID: "h", Action: ActionHealth},
		{ID: "g", Action: ActionHTTPGet, Path: "/index.html"},
	} {
		r := runOneStep(t, fs.URL, "tok", step)
		if got := r.Steps[0].Fields.Body; got != NonJSONBody {
			t.Fatalf("%s body = %#v, want %q", step.Action, got, NonJSONBody)
		}
	}
}

func TestTransportFailureIsRecordedNotFatal(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{}`))
	url := fs.URL
	fs.Close()

	r := runOneStep(t, url, "tok", Step{ID: "h", Action: ActionHealth})
	if r.Status != StatusCompleted {
		t.Fatalf("status = %s, want completed (a dead server is an observation)", r.Status)
	}
	f := r.Steps[0].Fields
	if f.Status != nil || f.OK || f.Error == nil || f.Body != nil {
		t.Fatalf("fields = %+v", f)
	}
}

func TestDriverFailuresEndTheRun(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{}`))
	cases := []struct {
		name string
		step Step
		want string
	}{
		{"unknown action", Step{ID: "x", Action: "teleport"}, `unknown action "teleport"`},
		{"unknown auth", Step{ID: "x", Action: ActionHealth, Auth: "sideways"}, `unknown auth mode "sideways"`},
		{"get_session without id", Step{ID: "x", Action: ActionGetSession}, "requires session_id"},
		{"snapshot without id", Step{ID: "x", Action: ActionSessionSnapshot}, "requires session_id"},
		{"http_get without path", Step{ID: "x", Action: ActionHTTPGet}, "requires path"},
		{"http_post without path", Step{ID: "x", Action: ActionHTTPPost}, "requires path"},
		{"unformable path", Step{ID: "x", Action: ActionHTTPGet, Path: "/a\nb"}, "cannot request"},
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
			// The failing step is still reported, so the harness sees which one.
			if len(r.Steps) != 1 || r.Steps[0].ID != "x" || r.Steps[0].Fields.Error == nil {
				t.Fatalf("steps = %+v", r.Steps)
			}
		})
	}
}

func TestRunStopsAtTheFirstDriverFailure(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{}`))
	sc := &Scenario{ID: "010_t", Steps: []Step{
		{ID: "first", Action: ActionHealth},
		{ID: "broken", Action: "teleport"},
		{ID: "never", Action: ActionHealth},
	}}
	r := RunScenario(context.Background(), sc, ClientOptions{BaseURL: fs.URL})
	if r.Status != StatusError || len(r.Steps) != 2 {
		t.Fatalf("status=%s steps=%d, want error with 2 steps", r.Status, len(r.Steps))
	}
	if r.Error == nil || !strings.Contains(*r.Error, "step broken:") {
		t.Fatalf("error should name the step: %v", r.Error)
	}
}

func TestUnsupportedCapability(t *testing.T) {
	sc := &Scenario{ID: "010_t", Requires: []string{"hijack.rest", "time.travel"},
		Steps: []Step{{ID: "h", Action: ActionHealth}}}
	r := RunScenario(context.Background(), sc, ClientOptions{BaseURL: "http://example.invalid"})
	if r.Status != StatusUnsupported {
		t.Fatalf("status = %s, want unsupported", r.Status)
	}
	if len(r.Steps) != 0 {
		t.Fatalf("an unsupported scenario runs no steps, got %+v", r.Steps)
	}
	if r.Error == nil || !strings.Contains(*r.Error, "time.travel") {
		t.Fatalf("error should name the missing capability: %v", r.Error)
	}
}

func TestBaseURLTrailingSlashIsTrimmed(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{}`))
	r := runOneStep(t, fs.URL+"/", "tok", Step{ID: "g", Action: ActionHTTPGet, Path: "/api/health"})
	if r.Steps[0].Fields.Status == nil || *r.Steps[0].Fields.Status != 200 {
		t.Fatalf("fields = %+v", r.Steps[0].Fields)
	}
	if got := fs.requests()[0].path; got != "/api/health" {
		t.Fatalf("path = %q, want /api/health", got)
	}
}

func TestRunClientWritesOneLine(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"status":"ok"}`))
	path := filepath.Join(t.TempDir(), "010_health.json")
	body := `{"id":"010_health","title":"h","steps":[{"id":"health","action":"health"}]}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	var out bytes.Buffer
	if err := RunClient(context.Background(), ClientOptions{
		BaseURL: fs.URL, Token: "tok", ScenarioPath: path,
	}, &out); err != nil {
		t.Fatalf("RunClient: %v", err)
	}
	if strings.Count(out.String(), "\n") != 1 {
		t.Fatalf("not one line: %q", out.String())
	}
	var r Result
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if r.ScenarioID != "010_health" || r.Status != StatusCompleted || len(r.Steps) != 1 {
		t.Fatalf("result = %+v", r)
	}
}

func TestRunClientStillReportsAnUnreadableScenario(t *testing.T) {
	var out bytes.Buffer
	path := filepath.Join(t.TempDir(), "042_missing.json")
	if err := RunClient(context.Background(), ClientOptions{ScenarioPath: path}, &out); err != nil {
		t.Fatalf("RunClient: %v", err)
	}
	var r Result
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if r.Status != StatusError || r.ScenarioID != "042_missing" || r.Error == nil {
		t.Fatalf("result = %+v", r)
	}
}

func TestRunClientSurfacesAWriteFailure(t *testing.T) {
	path := filepath.Join(t.TempDir(), "042_missing.json")
	if err := RunClient(context.Background(), ClientOptions{ScenarioPath: path}, errWriter{}); err == nil {
		t.Fatal("expected the write failure to surface")
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// fakeAuth resolves a principal from the X-Subject / X-Role test headers. An
// empty X-Subject yields the anonymous principal.
type fakeAuth struct{}

func (fakeAuth) Authenticate(_ context.Context, req *serverauth.Request) (*serverauth.Principal, error) {
	subject := req.Header("x-subject")
	if subject == "" {
		return serverauth.AnonymousPrincipal(), nil
	}
	role := req.Header("x-role")
	if role == "" {
		role = "viewer"
	}
	return &serverauth.Principal{
		SubjectID: subject,
		Roles:     serverauth.NewSet(role),
		Scopes:    serverauth.NewSet("*"),
	}, nil
}

// fakeRegistry is an in-memory SessionRegistry for tests.
type fakeRegistry struct {
	mu       sync.Mutex
	defs     map[string]*serverconfig.SessionDefinition
	statuses map[string]*SessionStatus
	// hooks override behavior for error-branch coverage.
	createErr   error
	updateErr   error
	annotateErr error
	controlErr  error
	snapErr     error
	analysis    map[string]any
	snapshot    map[string]any
	events      []map[string]any
	watch       map[string]any
	// created / stopped capture create/stop calls for the PAM integration tests.
	created []map[string]any
	stopped []string
}

func newFakeRegistry() *fakeRegistry {
	return &fakeRegistry{
		defs:     map[string]*serverconfig.SessionDefinition{},
		statuses: map[string]*SessionStatus{},
	}
}

func (f *fakeRegistry) add(id, owner, visibility string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	ownerCopy := owner
	f.defs[id] = &serverconfig.SessionDefinition{SessionID: id, Owner: &ownerCopy, Visibility: visibility, ConnectorType: "shell"}
	f.statuses[id] = &SessionStatus{SessionID: id, DisplayName: id, ConnectorType: "shell", LifecycleState: "running", InputMode: "hijack", Visibility: visibility, Owner: &ownerCopy, Tags: []string{}}
}

func (f *fakeRegistry) GetDefinition(_ context.Context, id string) (*serverconfig.SessionDefinition, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, ok := f.defs[id]
	return d, ok
}

func (f *fakeRegistry) ListWithDefinitions(context.Context) []SessionListItem {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]SessionListItem, 0, len(f.defs))
	for id, d := range f.defs {
		out = append(out, SessionListItem{Status: f.statuses[id], Definition: d})
	}
	return out
}

func (f *fakeRegistry) GetSession(_ context.Context, id string) (*SessionStatus, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if st, ok := f.statuses[id]; ok {
		return st, nil
	}
	return nil, ErrSessionNotFound
}

func (f *fakeRegistry) CreateSession(_ context.Context, payload map[string]any) (*SessionStatus, error) {
	if f.createErr != nil {
		return nil, f.createErr
	}
	id, _ := payload["session_id"].(string)
	if id == "" {
		id = "created"
	}
	st := &SessionStatus{SessionID: id, DisplayName: id, ConnectorType: stringField(payload, "connector_type"), LifecycleState: "waiting", InputMode: "hijack", Visibility: "private", Tags: []string{}}
	f.mu.Lock()
	f.statuses[id] = st
	f.created = append(f.created, payload)
	f.mu.Unlock()
	return st, nil
}

func (f *fakeRegistry) UpdateSession(_ context.Context, id string, _ map[string]any) (*SessionStatus, error) {
	if f.updateErr != nil {
		return nil, f.updateErr
	}
	return f.status(id)
}

func (f *fakeRegistry) DeleteSession(_ context.Context, id string) error {
	f.mu.Lock()
	delete(f.defs, id)
	delete(f.statuses, id)
	f.mu.Unlock()
	return nil
}

func (f *fakeRegistry) StartSession(_ context.Context, id string) (*SessionStatus, error) {
	return f.controlStatus(id)
}
func (f *fakeRegistry) StopSession(_ context.Context, id string) (*SessionStatus, error) {
	f.mu.Lock()
	f.stopped = append(f.stopped, id)
	f.mu.Unlock()
	return f.controlStatus(id)
}
func (f *fakeRegistry) RestartSession(_ context.Context, id string) (*SessionStatus, error) {
	return f.controlStatus(id)
}
func (f *fakeRegistry) SetMode(_ context.Context, id, _ string) (*SessionStatus, error) {
	return f.controlStatus(id)
}
func (f *fakeRegistry) ClearSession(_ context.Context, id string) (*SessionStatus, error) {
	return f.controlStatus(id)
}

func (f *fakeRegistry) controlStatus(id string) (*SessionStatus, error) {
	if f.controlErr != nil {
		return nil, f.controlErr
	}
	return f.status(id)
}

func (f *fakeRegistry) status(id string) (*SessionStatus, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if st, ok := f.statuses[id]; ok {
		return st, nil
	}
	return nil, ErrSessionNotFound
}

func (f *fakeRegistry) AnalyzeSession(_ context.Context, id string) (map[string]any, error) {
	if _, ok := f.GetDefinition(context.Background(), id); !ok {
		return nil, ErrSessionNotFound
	}
	if f.analysis == nil {
		return map[string]any{}, nil
	}
	return f.analysis, nil
}

func (f *fakeRegistry) LastSnapshot(context.Context, string) (map[string]any, error) {
	return f.snapshot, f.snapErr
}

func (f *fakeRegistry) Events(context.Context, string, int) ([]map[string]any, error) {
	if f.events == nil {
		return []map[string]any{}, nil
	}
	return f.events, nil
}

func (f *fakeRegistry) WatchSessionEvents(context.Context, string, WatchParams) (map[string]any, error) {
	if f.watch == nil {
		return map[string]any{}, nil
	}
	return f.watch, nil
}

func (f *fakeRegistry) AnnotateSession(context.Context, string, Annotation) (float64, int, error) {
	if f.annotateErr != nil {
		return 0, 0, f.annotateErr
	}
	return 1.5, 7, nil
}

// testServer bundles a Server with its fakes for assertions.
type testServer struct {
	srv     *Server
	reg     *fakeRegistry
	hub     *hub.TermHub
	metrics *Metrics
	apiKeys *serverauth.ApiKeyStore
}

// newTestServer builds a Server backed by a real hub + fakes. opt lets a test
// tweak config/deps before New.
func newTestServer(t *testing.T, opt func(cfg *serverconfig.UtermServerConfig, deps *Deps)) *testServer {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	cfg.Auth.APIKeysEnabled = true // pragma: allowlist secret
	metrics := NewMetrics()
	clock := hub.NewRealClock()
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	bus := hub.NewEventBus(hub.EventBusOptions{})
	h := hub.NewTermHub(hub.TermHubConfig{Clock: clock, OnMetric: metrics.Inc, Logger: quiet, EventBus: bus})
	reg := newFakeRegistry()
	apiKeys := serverauth.NewApiKeyStore()
	deps := Deps{
		Hub:      h,
		Auth:     fakeAuth{},
		Authz:    serverauth.NewAuthorizationService(),
		Config:   cfg,
		Registry: reg,
		APIKeys:  apiKeys,
		Metrics:  metrics,
		Clock:    clock,
		Version:  "9.9.9",
		Logger:   quiet,
	}
	if opt != nil {
		opt(cfg, &deps)
	}
	srv, err := New(deps)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return &testServer{srv: srv, reg: reg, hub: h, metrics: metrics, apiKeys: apiKeys}
}

// do issues a request against the wrapped handler and returns the recorder.
func (ts *testServer) do(method, target string, body string, headers map[string]string) *httptest.ResponseRecorder {
	var req *http.Request
	if body != "" {
		req = httptest.NewRequest(method, target, strings.NewReader(body))
	} else {
		req = httptest.NewRequest(method, target, http.NoBody)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	ts.srv.Handler().ServeHTTP(rec, req)
	return rec
}

// admin/viewer header sets.
func adminHeaders() map[string]string {
	return map[string]string{"X-Subject": "admin1", "X-Role": "admin"}
}
func viewerHeaders() map[string]string {
	return map[string]string{"X-Subject": "view1", "X-Role": "viewer"}
}

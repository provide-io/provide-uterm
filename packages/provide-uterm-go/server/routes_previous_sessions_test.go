//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// GET /api/previous-sessions[/{id}]: the capture sessions that have ended,
// read exactly as the live session was. Port of test_previous_sessions.py.

const (
	alicePrivate  = "pam-alice-capture-1-10"
	aliceOperator = "pam-alice-capture-2-10"
	bobPrivate    = "pam-bob-capture-3-10"
	bobPublic     = "pam-bob-capture-4-10"
)

func previousDefinition(id, owner, visibility string) serverconfig.SessionDefinition {
	return serverconfig.SessionDefinition{
		SessionID: id, DisplayName: owner + " (pts/1) [live]", ConnectorType: "pty_capture",
		ConnectorConfig: map[string]any{"socket_path": "/run/c/s.sock"}, InputMode: "open",
		Owner: &owner, Visibility: visibility, CreatedAt: time.Now().UTC(),
		AutoTransferIdleS: 30, KeystrokeQueue: "display",
	}
}

// endPrevious leaves a previous session that ended secondsAgo, with its screen
// and two rows of scrollback.
func endPrevious(store *capturestate.Store, id, owner, visibility, screen string, secondsAgo float64) {
	store.EndRecord(&capturestate.Record{
		Definition: previousDefinition(id, owner, visibility), RecordingInstance: "i",
		Screen: &screen, Cols: 90, Rows: 30, Scrollback: []string{"one", "two"},
		SavedAt: float64(time.Now().UnixNano())/1e9 - secondsAgo,
	})
}

// previousStore is a store in a private directory, as Build makes one.
func previousStoreIn(t *testing.T, persist bool, keepDays int) (*capturestate.Store, string) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "capture-sessions")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	return capturestate.New(dir, persist, keepDays, 100, slog.New(slog.NewTextHandler(io.Discard, nil))), dir
}

func previousServer(t *testing.T, keepDays int, opts ...func(*serverconfig.UtermServerConfig, *Deps)) (*testServer, string) {
	t.Helper()
	store, dir := previousStoreIn(t, false, 7)
	endPrevious(store, alicePrivate, "alice", "private", "alice's private screen", 30)
	endPrevious(store, aliceOperator, "alice", "operator", "alice's operator screen", 20)
	endPrevious(store, bobPrivate, "bob", "private", "bob's screen", 10)
	endPrevious(store, bobPublic, "bob", "public", "bob's public screen", 5)
	if keepDays == 0 {
		store = capturestate.New(dir, true, 0, 0, slog.New(slog.NewTextHandler(io.Discard, nil)))
	}
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.SessionPersistence.PreviousKeepDays = keepDays
		deps.CaptureState = store
		for _, opt := range opts {
			opt(cfg, deps)
		}
	})
	ts.srv.MarkReady()
	return ts, dir
}

func as(subject, role string) map[string]string {
	return map[string]string{"X-Subject": subject, "X-Role": role}
}

func listed(t *testing.T, ts *testServer, headers map[string]string) []string {
	t.Helper()
	rec := ts.do("GET", "/api/previous-sessions", "", headers)
	if rec.Code != http.StatusOK {
		t.Fatalf("list: %d %s", rec.Code, rec.Body.String())
	}
	var body struct {
		Sessions []map[string]any `json:"sessions"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body.Sessions == nil {
		t.Fatalf("list body %s: %v", rec.Body.String(), err)
	}
	ids := []string{}
	for _, s := range body.Sessions {
		ids = append(ids, s["id"].(string))
	}
	return ids
}

func TestPreviousSessionsAreListedToWhoeverCouldReadThem(t *testing.T) {
	ts, _ := previousServer(t, 7)
	for _, tc := range []struct {
		headers map[string]string
		want    []string
	}{
		{as("alice", "viewer"), []string{aliceOperator, alicePrivate}},
		{as("carol", "operator"), []string{bobPublic, aliceOperator}},
		// History is read by fewer than the live session: not even a public one.
		{as("dave", "viewer"), []string{}},
		{as("root", "admin"), []string{bobPublic, bobPrivate, aliceOperator, alicePrivate}},
		{map[string]string{"X-Subject": "scoped", "X-Role": "admin", "X-Admin-Session-Scope": alicePrivate}, []string{alicePrivate}},
	} {
		if got := listed(t, ts, tc.headers); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%v: listed %v, want %v", tc.headers, got, tc.want)
		}
	}
}

func TestAListingCarriesTheSummaryFieldsOnly(t *testing.T) {
	ts, _ := previousServer(t, 7)
	rec := ts.do("GET", "/api/previous-sessions", "", as("bob", "viewer"))
	var body struct {
		Sessions []map[string]any `json:"sessions"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	entry := body.Sessions[1]
	ended, _ := entry["ended_at"].(float64)
	delete(entry, "ended_at")
	want := map[string]any{"id": bobPrivate, "display_name": "bob (pts/1) [live]", "owner": "bob", "cols": float64(90), "rows": float64(30)}
	if !reflect.DeepEqual(entry, want) || ended <= 0 {
		t.Fatalf("entry %v ended %v", entry, ended)
	}
}

func TestTheOwnerReadsTheScreenAndScrollback(t *testing.T) {
	ts, _ := previousServer(t, 7)
	rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("alice", "viewer"))
	if rec.Code != http.StatusOK {
		t.Fatalf("read: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["id"] != alicePrivate || body["screen"] != "alice's private screen" ||
		!reflect.DeepEqual(body["scrollback"], []any{"one", "two"}) || len(body) != 8 {
		t.Fatalf("body %v", body)
	}
	rec = ts.do("GET", "/api/previous-sessions/"+aliceOperator, "", as("carol", "operator"))
	if rec.Code != http.StatusOK || decode(t, rec.Body.Bytes())["screen"] != "alice's operator screen" {
		t.Fatalf("operator read: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAPreviousSessionTheCallerCouldNotReadIs403(t *testing.T) {
	ts, _ := previousServer(t, 7)
	for _, tc := range []struct {
		id      string
		headers map[string]string
	}{
		{alicePrivate, as("carol", "operator")},
		{alicePrivate, as("dave", "viewer")},
		{aliceOperator, as("dave", "viewer")},
		{bobPrivate, as("alice", "viewer")},
		{bobPublic, as("dave", "viewer")},
		{bobPublic, as("alice", "viewer")},
		{bobPrivate, map[string]string{"X-Subject": "scoped", "X-Role": "admin", "X-Admin-Session-Scope": alicePrivate}},
	} {
		rec := ts.do("GET", "/api/previous-sessions/"+tc.id, "", tc.headers)
		if rec.Code != http.StatusForbidden || decode(t, rec.Body.Bytes())["detail"] != "insufficient privileges" {
			t.Errorf("%s as %v: %d %s", tc.id, tc.headers, rec.Code, rec.Body.String())
		}
	}
}

// A share viewer, a watch or a tunnel share, lists none and reads none, what
// its token: a grant is for the live stream. Refused as one unknown, so it
// learns nothing of what was kept.
func TestAShareViewerListsNoneAndReadsNone(t *testing.T) {
	ts, _ := previousServer(t, 7)
	for _, headers := range []map[string]string{
		as("share:"+alicePrivate+":blackbetty:7", "viewer"),
		as("share:"+alicePrivate+":viewer", "viewer"),
		as("share:"+bobPublic+":operator", "operator"),
		{"X-Subject": "share:" + alicePrivate + ":operator", "X-Role": "admin", "X-Admin-Session-Scope": alicePrivate},
	} {
		if got := listed(t, ts, headers); len(got) != 0 {
			t.Errorf("%v listed %v", headers, got)
		}
		for _, id := range []string{alicePrivate, bobPublic} {
			if rec := ts.do("GET", "/api/previous-sessions/"+id, "", headers); rec.Code != http.StatusForbidden {
				t.Errorf("%v read %s: %d", headers, id, rec.Code)
			}
		}
	}
}

// policyRoles decides roles itself, as a webhook does: "policy-admin" is admin
// without the role in its token, and "root" a viewer with it.
type policyRoles struct {
	serverauth.LocalAuthorizationProvider
}

func (policyRoles) CanReadSession(p *serverauth.Principal, _ *serverconfig.SessionDefinition) bool {
	return p.SubjectID == "policy-admin" || p.SubjectID == "root"
}

func (policyRoles) IsAdmin(p *serverauth.Principal) bool { return p.SubjectID == "policy-admin" }

func (r policyRoles) HasCapability(p *serverauth.Principal, capability string) bool {
	return p.SubjectID == "policy-admin" || r.LocalAuthorizationProvider.HasCapability(p, capability)
}

func TestAdminIsTheRoleTheProviderGives(t *testing.T) {
	ts, _ := previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Authz = serverauth.NewAuthorizationServiceWith(policyRoles{})
	})
	if got := listed(t, ts, as("policy-admin", "viewer")); !reflect.DeepEqual(got, []string{bobPublic, bobPrivate, aliceOperator, alicePrivate}) {
		t.Fatalf("policy admin listed %v", got)
	}
	if rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("policy-admin", "viewer")); rec.Code != http.StatusOK {
		t.Fatalf("policy admin read: %d", rec.Code)
	}
	if got := listed(t, ts, as("root", "admin")); len(got) != 0 {
		t.Fatalf("a role the provider withholds listed %v", got)
	}
	if rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("root", "admin")); rec.Code != http.StatusForbidden {
		t.Fatalf("a role the provider withholds read: %d", rec.Code)
	}
}

// countingAuthz is the local provider, counting each call the service makes
// of it: under a webhook provider each is a round trip.
type countingAuthz struct {
	serverauth.LocalAuthorizationProvider
	calls map[string]int
}

func (c countingAuthz) IsAdmin(p *serverauth.Principal) bool {
	c.calls["standing"]++
	return c.LocalAuthorizationProvider.IsAdmin(p)
}

func (c countingAuthz) HasCapability(p *serverauth.Principal, capability string) bool {
	c.calls["standing"]++
	return c.LocalAuthorizationProvider.HasCapability(p, capability)
}

func (c countingAuthz) ResolveBrowserRole(p *serverauth.Principal, session *serverconfig.SessionDefinition) string {
	c.calls["standing"]++
	return c.LocalAuthorizationProvider.ResolveBrowserRole(p, session)
}

func (c countingAuthz) CanReadSession(p *serverauth.Principal, session *serverconfig.SessionDefinition) bool {
	c.calls["can_read_session"]++
	return c.LocalAuthorizationProvider.CanReadSession(p, session)
}

// What the caller is (admin, operator) is asked once a request; only the
// live read's own per-session gate runs per record.
func TestAListingAsksForTheCallersStandingOnceNotPerRecord(t *testing.T) {
	calls := map[string]int{}
	ts, _ := previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Authz = serverauth.NewAuthorizationServiceWith(countingAuthz{calls: calls})
	})
	for _, headers := range []map[string]string{as("root", "admin"), as("carol", "operator"), as("dave", "viewer")} {
		clear(calls)
		listed(t, ts, headers)
		if calls["standing"] > 2 || calls["can_read_session"] != 4 {
			t.Errorf("%v: calls %v", headers, calls)
		}
	}
}

// secretGate hides "secret" from viewers, counting each time it is asked, or
// fails when told to.
type secretGate struct {
	fail  bool
	asked *int
}

func (g secretGate) GetRedactionRules(_ context.Context, pc hub.PolicyContext) ([]hub.RedactionRule, error) {
	*g.asked++
	if g.fail {
		return nil, errors.New("policy service down")
	}
	if pc.Role != nil && *pc.Role == "viewer" {
		return []hub.RedactionRule{{Pattern: "secret", Replacement: "[REDACTED]"}}, nil
	}
	return nil, nil
}

func gatedServer(t *testing.T, gate secretGate) *testServer {
	t.Helper()
	store, _ := previousStoreIn(t, false, 7)
	screen := "a secret here"
	store.EndRecord(&capturestate.Record{
		Definition: previousDefinition(alicePrivate, "alice", "private"), RecordingInstance: "i",
		Screen: &screen, Cols: 80, Rows: 24, Scrollback: []string{"secret one", "two"},
		SavedAt: float64(time.Now().Unix()),
	})
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, deps *Deps) {
		cfg.SessionPersistence.PreviousKeepDays = 7
		deps.CaptureState = store
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock: deps.Clock, OutputPolicyGate: gate, Redactor: hub.RedactFrameFields,
			OnMetric: deps.Metrics.Inc, Logger: deps.Logger,
		})
	})
	ts.srv.MarkReady()
	return ts
}

func TestTheScreenAndScrollbackAreRedactedAsTheLiveScreenIs(t *testing.T) {
	asked := 0
	ts := gatedServer(t, secretGate{asked: &asked})
	viewer := decode(t, ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("alice", "viewer")).Body.Bytes())
	admin := decode(t, ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("root", "admin")).Body.Bytes())
	if viewer["screen"] != "a [REDACTED] here" || !reflect.DeepEqual(viewer["scrollback"], []any{"[REDACTED] one", "two"}) {
		t.Fatalf("viewer %v", viewer)
	}
	if admin["screen"] != "a secret here" || !reflect.DeepEqual(admin["scrollback"], []any{"secret one", "two"}) {
		t.Fatalf("admin %v", admin)
	}
	// Once a read, for the screen and every row of scrollback together.
	if asked != 2 {
		t.Fatalf("the policy was asked %d times", asked)
	}
}

func TestAPolicyThatCannotAnswerWithholdsTheScreen(t *testing.T) {
	asked := 0
	ts := gatedServer(t, secretGate{fail: true, asked: &asked})
	rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("alice", "viewer"))
	if rec.Code != http.StatusServiceUnavailable || decode(t, rec.Body.Bytes())["detail"] != "output policy unavailable" {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}

func TestAnUnknownOrVanishedPreviousSessionIs404(t *testing.T) {
	ts, dir := previousServer(t, 7)
	rec := ts.do("GET", "/api/previous-sessions/pam-alice-capture-9-9", "", as("root", "admin"))
	if rec.Code != http.StatusNotFound || decode(t, rec.Body.Bytes())["detail"] != "unknown previous session: pam-alice-capture-9-9" {
		t.Fatalf("unknown: %d %s", rec.Code, rec.Body.String())
	}
	_ = os.Remove(filepath.Join(dir, "previous", alicePrivate+".json"))
	rec = ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("alice", "viewer"))
	if rec.Code != http.StatusNotFound || decode(t, rec.Body.Bytes())["detail"] != "unknown previous session: "+alicePrivate {
		t.Fatalf("vanished: %d %s", rec.Code, rec.Body.String())
	}
}

func TestPreviousSessionsNeedACallerAndASafeId(t *testing.T) {
	ts, _ := previousServer(t, 7)
	if rec := ts.do("GET", "/api/previous-sessions", "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous list: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", nil); rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous read: %d", rec.Code)
	}
	if rec := ts.do("GET", "/api/previous-sessions/a.b", "", as("root", "admin")); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("unsafe id: %d", rec.Code)
	}
}

func TestPreviousSessionsOffListNoneAndReadNone(t *testing.T) {
	for name, opt := range map[string]func(*serverconfig.UtermServerConfig, *Deps){
		"no store": nil,
		"kept none": func(_ *serverconfig.UtermServerConfig, deps *Deps) {
			deps.CaptureState = capturestate.New(t.TempDir(), true, 0, 0, slog.New(slog.NewTextHandler(io.Discard, nil)))
		},
	} {
		ts := newTestServer(t, opt)
		if got := listed(t, ts, as("root", "admin")); len(got) != 0 {
			t.Errorf("%s: listed %v", name, got)
		}
		if rec := ts.do("GET", "/api/previous-sessions/"+alicePrivate, "", as("root", "admin")); rec.Code != http.StatusNotFound {
			t.Errorf("%s: read %d", name, rec.Code)
		}
	}
}

func TestHealthListsEachSessionFeatureOnlyWhenItIsOn(t *testing.T) {
	for _, tc := range []struct {
		persist bool
		days    int
		want    []any
	}{
		{false, 0, []any{"viewers.revoke.subject"}},
		{true, 0, []any{"viewers.revoke.subject", "sessions.capture.persist"}},
		{false, 7, []any{"viewers.revoke.subject", "sessions.previous"}},
		{true, 7, []any{"viewers.revoke.subject", "sessions.capture.persist", "sessions.previous"}},
	} {
		ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
			cfg.SessionPersistence.PersistCapture = tc.persist
			cfg.SessionPersistence.PreviousKeepDays = tc.days
		})
		ts.srv.MarkReady()
		if got := decode(t, ts.do("GET", "/api/health", "", nil).Body.Bytes())["features"]; !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%+v: features %v", tc, got)
		}
	}
}

// A restored login is alive only while its pid reports the start ticks its
// id names; a pid reused by another login, or gone, is not.
func TestAPamLoginIsAliveOnlyWhileItsPidReportsItsStart(t *testing.T) {
	defer func(orig func(int) (int64, bool)) { pamProcessStartTicks = orig }(pamProcessStartTicks)
	for _, tc := range []struct {
		ticks int64
		ok    bool
		alive bool
	}{{777, true, true}, {778, true, false}, {777, false, false}} {
		pamProcessStartTicks = func(int) (int64, bool) { return tc.ticks, tc.ok }
		if got := PamLoginAlive(4242, 777); got != tc.alive {
			t.Errorf("%+v: alive = %v", tc, got)
		}
	}
}

// The confinement a restore applies is the one PAM's open applies.
func TestCaptureSocketConfinementIsTheOnePamApplies(t *testing.T) {
	dir := "/run/uterm-capture"
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := serverconfig.PamConfig{CaptureSocketDir: &dir}
	if !CaptureSocketConfined(cfg, dir+"/uterm-cap-1.sock", quiet) || CaptureSocketConfined(cfg, "/run/evil.sock", quiet) {
		t.Fatal("confinement differs")
	}
	if !CaptureSocketConfined(serverconfig.PamConfig{}, "/anywhere.sock", quiet) {
		t.Fatal("no base confines")
	}
}

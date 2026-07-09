//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"errors"
	"sync"
	"testing"
)

// --- test doubles ---

type broadcastCall struct {
	workerID string
	msg      map[string]any
}

type fakeHub struct {
	mu    sync.Mutex
	calls []broadcastCall
	errAt int // 1-based call index that returns an error (0 = never)
	n     int
}

func (h *fakeHub) Broadcast(workerID string, msg map[string]any) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.n++
	h.calls = append(h.calls, broadcastCall{workerID, msg})
	if h.errAt != 0 && h.n == h.errAt {
		return errors.New("boom")
	}
	return nil
}

func (h *fakeHub) count() int { h.mu.Lock(); defer h.mu.Unlock(); return len(h.calls) }
func (h *fakeHub) last() broadcastCall {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.calls[len(h.calls)-1]
}
func (h *fakeHub) reset() { h.mu.Lock(); defer h.mu.Unlock(); h.calls = nil; h.n = 0 }

type fakeWS struct{ AnonConn }

type fakePrincipal struct {
	subjectID   string
	displayName string
}

func (p fakePrincipal) SubjectID() string   { return p.subjectID }
func (p fakePrincipal) DisplayName() string { return p.displayName }

type subjectOnlyPrincipal struct{ subjectID string }

func (p subjectOnlyPrincipal) SubjectID() string { return p.subjectID }

type noSubjectPrincipal struct{}

func newService() (*DeckMuxPresence, *fakeHub) {
	h := &fakeHub{}
	return NewDeckMuxPresence(h), h
}

func firstUser(t *testing.T, result map[string]any) map[string]any {
	t.Helper()
	users, ok := result["users"].([]map[string]any)
	if !ok || len(users) == 0 {
		t.Fatalf("no users in result: %v", result)
	}
	return users[0]
}

// --- connect ---

func TestConnectSendsSync(t *testing.T) {
	d, h := newService()
	result, err := d.OnBrowserConnect("w1", &fakeWS{}, "operator", nil)
	if err != nil {
		t.Fatal(err)
	}
	if result["type"] != "presence_sync" {
		t.Error("type")
	}
	if firstUser(t, result)["role"] != "operator" {
		t.Error("role")
	}
	if _, ok := result["config"]; !ok {
		t.Error("config missing")
	}
	if h.count() != 0 { // first connect does not broadcast
		t.Error("first connect broadcast")
	}
}

func TestConnectConfigValues(t *testing.T) {
	d, _ := newService()
	result, _ := d.OnBrowserConnect("w1", &fakeWS{}, "viewer", nil)
	cfg := result["config"].(map[string]any)
	if cfg["auto_transfer_idle_s"] != 30 || cfg["keystroke_queue"] != "display" {
		t.Errorf("config = %v", cfg)
	}
}

func TestSecondConnectBroadcastsSync(t *testing.T) {
	d, h := newService()
	_, _ = d.OnBrowserConnect("w1", &fakeWS{}, "viewer", nil)
	if h.count() != 0 {
		t.Fatal("premature broadcast")
	}
	result, _ := d.OnBrowserConnect("w1", &fakeWS{}, "operator", nil)
	if len(result["users"].([]map[string]any)) != 2 {
		t.Error("two users")
	}
	if h.count() != 1 || h.last().workerID != "w1" || h.last().msg["type"] != "presence_sync" {
		t.Error("second connect must broadcast sync")
	}
}

func TestConnectColorsAvoidCollision(t *testing.T) {
	d, _ := newService()
	r1, _ := d.OnBrowserConnect("w1", &fakeWS{}, "admin", nil)
	r2, _ := d.OnBrowserConnect("w1", &fakeWS{}, "viewer", nil)
	users := r2["users"].([]map[string]any)
	if firstUser(t, r1)["color"] == users[1]["color"] {
		t.Error("colors collided")
	}
}

func TestConnectPrincipalVariants(t *testing.T) {
	d, _ := newService()

	// Full principal → display name used, subject as user_id, initials.
	r, _ := d.OnBrowserConnect("w1", &fakeWS{}, "admin", fakePrincipal{"user-123", "Alice Smith"})
	u := firstUser(t, r)
	if u["name"] != "Alice Smith" || u["user_id"] != "user-123" || u["initials"] != "AS" {
		t.Errorf("full principal: %v", u)
	}

	// Subject-only principal (no DisplayName) → name = subject.
	r, _ = d.OnBrowserConnect("w2", &fakeWS{}, "operator", subjectOnlyPrincipal{"svc-456"})
	u = firstUser(t, r)
	if u["name"] != "svc-456" || u["user_id"] != "svc-456" {
		t.Errorf("subject-only: %v", u)
	}

	// Empty display name → falls back to subject.
	r, _ = d.OnBrowserConnect("w3", &fakeWS{}, "operator", fakePrincipal{"sre:carol", ""})
	if firstUser(t, r)["name"] != "sre:carol" {
		t.Error("empty display name fallback")
	}

	// Truthy principal without subject_id → anonymous generated name.
	ws := &fakeWS{}
	r, _ = d.OnBrowserConnect("w4", ws, "viewer", noSubjectPrincipal{})
	u = firstUser(t, r)
	if u["name"] == "" || u["user_id"] != ws.DeckMuxAnonID() {
		t.Errorf("no-subject principal: %v", u)
	}
}

func TestConnectAnonymous(t *testing.T) {
	d, _ := newService()
	ws := &fakeWS{}
	r, _ := d.OnBrowserConnect("w1", ws, "viewer", nil)
	u := firstUser(t, r)
	if u["name"] == "" || u["user_id"] != ws.DeckMuxAnonID() {
		t.Error("anonymous identity")
	}
	if len(u["initials"].(string)) != 2 {
		t.Errorf("initials = %q", u["initials"])
	}
}

func TestConnectPrunesIdle(t *testing.T) {
	withTimeNow(t, 1000.0)
	d, _ := newService()
	staleWS := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", staleWS, "viewer", nil)
	store := d.GetPresenceStore("w1")
	staleID := staleWS.DeckMuxAnonID()
	store.users[staleID].LastActivityAt = 900.0 // 100s idle > 30s

	newWS := &fakeWS{}
	r, _ := d.OnBrowserConnect("w1", newWS, "operator", nil)
	ids := map[string]bool{}
	for _, u := range r["users"].([]map[string]any) {
		ids[u["user_id"].(string)] = true
	}
	if ids[staleID] || !ids[newWS.DeckMuxAnonID()] {
		t.Errorf("prune failed: %v", ids)
	}
}

func TestConnectBroadcastError(t *testing.T) {
	h := &fakeHub{errAt: 1}
	d := NewDeckMuxPresence(h)
	_, _ = d.OnBrowserConnect("w1", &fakeWS{}, "viewer", nil)
	if _, err := d.OnBrowserConnect("w1", &fakeWS{}, "operator", nil); err == nil {
		t.Error("expected broadcast error")
	}
}

func TestConnectViaIdentityPrincipal(t *testing.T) {
	d, _ := newService()
	principal := IdentityAsPrincipal(&ResolvedIdentity{
		Subject: "sre:alice", Claims: map[string]any{"display_name": "Alice Smith"},
		Fingerprint: "SHA256:abc",
	})
	r, _ := d.OnBrowserConnect("w1", &fakeWS{}, "admin", principal)
	u := firstUser(t, r)
	if u["user_id"] != "sre:alice" || u["name"] != "Alice Smith" || u["initials"] != "AS" || u["role"] != "admin" {
		t.Errorf("identity principal: %v", u)
	}
}

// --- disconnect ---

func TestDisconnect(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	h.reset()

	if err := d.OnBrowserDisconnect("w1", ws, nil); err != nil {
		t.Fatal(err)
	}
	if h.count() != 1 || h.last().msg["type"] != "presence_leave" ||
		h.last().msg["user_id"] != ws.DeckMuxAnonID() {
		t.Errorf("leave broadcast: %v", h.calls)
	}
}

func TestDisconnectNotPresent(t *testing.T) {
	d, h := newService()
	if err := d.OnBrowserDisconnect("w1", &fakeWS{}, nil); err != nil {
		t.Fatal(err)
	}
	if h.count() != 0 {
		t.Error("no broadcast for absent user")
	}
}

func TestDisconnectAuthenticated(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	p := fakePrincipal{subjectID: "alice"}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", p)
	h.reset()
	_ = d.OnBrowserDisconnect("w1", ws, p)
	if h.last().msg["user_id"] != "alice" {
		t.Error("subject-keyed leave")
	}
}

func TestDisconnectWrongWSGhostAbsence(t *testing.T) {
	d, h := newService()
	p := fakePrincipal{subjectID: "bob"}
	_, _ = d.OnBrowserConnect("w1", &fakeWS{}, "operator", p)
	h.reset()
	// Disconnect a different ws with no principal → anon id not in store.
	_ = d.OnBrowserDisconnect("w1", &fakeWS{}, nil)
	if h.count() != 0 {
		t.Error("ghost disconnect broadcast")
	}
}

func TestDisconnectNoSubjectPrincipalSafe(t *testing.T) {
	d, _ := newService()
	if err := d.OnBrowserDisconnect("w1", &fakeWS{}, noSubjectPrincipal{}); err != nil {
		t.Fatal(err)
	}
}

func TestDisconnectBroadcastError(t *testing.T) {
	h := &fakeHub{}
	d := NewDeckMuxPresence(h)
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	h.errAt = h.n + 1
	if err := d.OnBrowserDisconnect("w1", ws, nil); err == nil {
		t.Error("expected error")
	}
}

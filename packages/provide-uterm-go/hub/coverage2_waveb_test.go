//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"testing"
)

func TestRegisterWorkerTrimsEvents(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.EventDequeMaxlen = 2 })
	st := NewWorkerTermState()
	for i := 0; i < 5; i++ {
		st.Events = append(st.Events, map[string]any{"seq": i})
	}
	h.registry.Put("w1", st)
	_, _ = h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, len(st.Events), 2, "events trimmed to maxlen on register")
}

func TestSetWorkerHelloLegacyProtocolWarn(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	pv := 0
	ok, _ := h.Conn.SetWorkerHello(bg(), "w1", InputModeHijack, &pv)
	mustTrue(t, ok, "legacy protocol still applies")
}

func TestBrowserPrincipalEmptySubjectExempt(t *testing.T) {
	mustEqual(t, browserPrincipalSubjectID(newBrowserWS("x")), (*string)(nil), "no principal exempt")
	ws := newBrowserWS("y")
	ws.principal = &Principal{SubjectID: ""}
	mustEqual(t, browserPrincipalSubjectID(ws), (*string)(nil), "empty subject exempt")
	ws2 := newBrowserWS("z")
	ws2.principal = "not-a-principal"
	mustEqual(t, browserPrincipalSubjectID(ws2), (*string)(nil), "non-principal exempt")
}

// countingFailStore succeeds on the first Create then fails, to drive the
// rollback path where the principal still has a remaining connection.
type countingFailStore struct {
	*InMemoryResumeStore
	n int
}

func (s *countingFailStore) Create(ctx context.Context, w, r string, ttl float64) (string, error) {
	s.n++
	if s.n >= 2 {
		return "", errors.New("second create fails")
	}
	return s.InMemoryResumeStore.Create(ctx, w, r, ttl)
}

func TestRollbackBrowserQuotaRemainingPositive(t *testing.T) {
	store := &countingFailStore{InMemoryResumeStore: NewInMemoryResumeStore(nil, seqTokenGen())}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.ResumeStore = store; c.MaxConnectionsPerPrincipal = 5 })
	a := newBrowserWS("a")
	a.principal = &Principal{SubjectID: "p"}
	b := newBrowserWS("b")
	b.principal = &Principal{SubjectID: "p"}
	_, err := h.Conn.RegisterBrowser(bg(), "w1", a, "viewer", false)
	mustEqual(t, err, nil, "first ok")
	_, err = h.Conn.RegisterBrowser(bg(), "w1", b, "viewer", false)
	if err == nil {
		t.Fatal("second register should fail")
	}
	h.lock.Lock()
	count := h.principalBrowserCounts["p"]
	h.lock.Unlock()
	mustEqual(t, count, 1, "rollback leaves the first connection counted")
}

func TestUpdateLockStatePrincipalRemainingPositive(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.MaxConnectionsPerPrincipal = 5 })
	a := newBrowserWS("a")
	a.principal = &Principal{SubjectID: "p"}
	b := newBrowserWS("b")
	b.principal = &Principal{SubjectID: "p"}
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", a, "viewer", false)
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", b, "viewer", false)
	// Disconnect one -> remaining count 1 (>0 branch).
	_, _ = h.Conn.CleanupBrowserDisconnect(bg(), "w1", a, false)
	h.lock.Lock()
	count := h.principalBrowserCounts["p"]
	h.lock.Unlock()
	mustEqual(t, count, 1, "one same-principal connection remains")
}

func TestCleanupExpiredHijackDashboard(t *testing.T) {
	sink := &recordingSink{}
	h, clk := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	worker := &fakeWorkerWS{}
	owner := newBrowserWS("o")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[owner] = "operator"
	st.HijackOwner = owner
	exp := clk.Monotonic() - 1 // expired
	st.HijackOwnerExpiresAt = &exp

	cleaned, _ := h.CleanupExpiredHijack(bg(), "w1")
	mustTrue(t, cleaned, "expired dashboard cleaned")
	found := false
	for _, e := range sink.events {
		if e.EventType == "hijack.expired" && e.Metadata["hijack_type"] == "dashboard" {
			found = true
		}
	}
	mustTrue(t, found, "dashboard expiry telemetry")
}

func TestAppendEventEnqueuesToBus(t *testing.T) {
	bus := NewEventBus(EventBusOptions{Logger: discardLogger()})
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.EventBus = bus })
	h.registry.Put("w1", NewWorkerTermState())
	sub, remove, _ := bus.Watch("w1", nil, nil)
	defer remove()
	_, _ = h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "x"})
	select {
	case ev := <-sub.Queue:
		mustEqual(t, ev["type"], "term", "event delivered to bus subscriber")
	default:
		t.Fatal("event not enqueued to bus")
	}
}

func TestHijackStateMsgForRestLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "operator"
	st.HijackSession = &HijackSession{HijackID: "h", LeaseExpiresAt: clk.Monotonic() + 100}
	frame := h.HijackStateMsgFor(bg(), "w1", ws)
	mustTrue(t, frame.Hijacked, "rest hijack reflected")
	mustEqual(t, *frame.Owner, "other", "rest owner is other")
	mustEqual(t, *frame.LeaseExpiresAt, 5100.0, "rest lease mono->wall")
}

func TestRedactSnapshotNoRedactorWithRules(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		// No Redactor wired.
	})
	snap := map[string]any{"screen": "raw"}
	out, err := h.Router.RedactSnapshotForRecipient(bg(), "w1", snap, newBrowserWS("r"))
	mustEqual(t, err, nil, "no err")
	mustEqual(t, out["screen"], "raw", "rules but no redactor -> unchanged (documented deviation)")
}

func TestGetRecentEventsFewerThanLimit(t *testing.T) {
	h, _ := newTestHub(t, nil)
	h.registry.Put("w1", NewWorkerTermState())
	for i := 0; i < 3; i++ {
		_, _ = h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "x"})
	}
	got := h.GetRecentEvents(bg(), "w1", 10)
	mustEqual(t, len(got), 3, "returns all when fewer than limit")
}

func TestRecordKeystrokeRingCap(t *testing.T) {
	h, clk := newTestHub(t, nil)
	src := newBrowserWS("s")
	for i := 0; i < keystrokeRingMax+10; i++ {
		clk.SetMonotonic(1000 + float64(i))
		h.Router.RecordKeystroke(src)
	}
	h.Router.keystrokeMu.Lock()
	n := len(h.Router.keystrokes[src])
	h.Router.keystrokeMu.Unlock()
	mustEqual(t, n, keystrokeRingMax, "keystroke ring capped at max")
}

func TestInvalidInputModeErrorMessage(t *testing.T) {
	e := &InvalidInputModeError{Mode: "bogus"}
	if e.Error() == "" {
		t.Fatal("error message should be non-empty")
	}
}

func TestNoOpPolicyGateAllows(t *testing.T) {
	d, err := (NoOpPolicyGate{}).InterceptInput(bg(), "data", PolicyContext{})
	mustEqual(t, err, nil, "no err")
	mustEqual(t, d.Action, "allow", "no-op gate allows")
}

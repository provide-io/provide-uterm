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

func TestBrowserPrincipalTypedNil(t *testing.T) {
	ws := newBrowserWS("x")
	ws.principal = (*Principal)(nil) // typed nil inside the interface
	mustEqual(t, browserPrincipalSubjectID(ws), (*string)(nil), "typed-nil principal exempt")
}

func TestRollbackNoPrincipal(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.ResumeStore = failResumeStore{} })
	ws := newBrowserWS("b") // no principal
	_, err := h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", false)
	if err == nil {
		t.Fatal("expected resume create error")
	}
	// No principal was counted; rollback hit the !ok early return.
	h.lock.Lock()
	_, tokPresent := h.wsToResumeToken[ws]
	h.lock.Unlock()
	mustFalse(t, tokPresent, "no token leaked")
}

func TestRegisterBrowserRedactsInitialSnapshot(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
	})
	st := registerWorkerState(h, "w1", &fakeWorkerWS{})
	st.LastSnapshot = map[string]any{"screen": "secret", "type": "snapshot"}
	ws := newBrowserWS("b")
	state, err := h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", false)
	mustEqual(t, err, nil, "no err")
	redacted := state["initial_snapshot"].(map[string]any)
	mustEqual(t, redacted["screen"], "[REDACTED]", "connect-time snapshot redacted")
	// Stored snapshot untouched.
	mustEqual(t, st.LastSnapshot["screen"], "secret", "stored snapshot untouched")
}

func TestRegisterBrowserRedactionError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{err: errors.New("gate down")}
	})
	st := registerWorkerState(h, "w1", &fakeWorkerWS{})
	st.LastSnapshot = map[string]any{"screen": "s"}
	_, err := h.Conn.RegisterBrowser(bg(), "w1", newBrowserWS("b"), "viewer", false)
	if err == nil {
		t.Fatal("expected redaction gate error to propagate")
	}
}

func TestRedactSnapshotNilGateDirect(t *testing.T) {
	h, _ := newTestHub(t, nil) // no output gate
	snap := map[string]any{"screen": "s"}
	out, err := h.Router.RedactSnapshotForRecipient(bg(), "w1", snap, newBrowserWS("r"))
	mustEqual(t, err, nil, "no err")
	mustEqual(t, out["screen"], "s", "nil gate returns snapshot unchanged")
}

func TestGetRecentEventsClampsTo500(t *testing.T) {
	h, _ := newTestHub(t, nil)
	st := NewWorkerTermState()
	for i := 0; i < 3; i++ {
		st.Events = append(st.Events, map[string]any{"seq": i})
	}
	h.registry.Put("w1", st)
	// limit above 500 is clamped; with only 3 events all are returned.
	got := h.GetRecentEvents(bg(), "w1", 600)
	mustEqual(t, len(got), 3, "clamp does not inflate small event set")
}

// plainConn is comparable but does NOT implement BrowserSender.
type plainConn struct{ id int }

func TestBroadcastNonSenderTreatedDead(t *testing.T) {
	h, _ := newTestHub(t, nil)
	bad := &plainConn{id: 1}
	st := NewWorkerTermState()
	st.Browsers[bad] = "viewer"
	h.registry.Put("w1", st)
	mustEqual(t, h.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "x"}), nil, "err")
	h.lock.Lock()
	_, present := st.Browsers[bad]
	h.lock.Unlock()
	mustFalse(t, present, "non-sender conn pruned as dead")
}

// errIdentityProvider makes preparePolicyContext fail (drives the payloadsByRole
// and broadcast error paths).
type errIdentityProvider struct{}

func (errIdentityProvider) ResolvePrincipal(context.Context, BrowserConn) (any, error) {
	return nil, errors.New("idp down")
}

func TestPayloadsByRolePrepareError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
		c.IdentityProvider = errIdentityProvider{}
	})
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)
	err := h.Broadcast(bg(), "w1", map[string]any{"type": "snapshot", "screen": "s"})
	if err == nil {
		t.Fatal("expected identity-provider error to propagate through payloadsByRole")
	}
}

func TestBroadcastHijackStateRestLease(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	st := registerWorkerState(h, "w1", worker)
	st.Browsers[ws] = "viewer"
	st.HijackSession = &HijackSession{HijackID: "h", LeaseExpiresAt: clk.Monotonic() + 100}
	mustEqual(t, h.BroadcastHijackState(bg(), "w1"), nil, "err")
	// viewFor's REST-lease branch used; browser sees hijacked + owner other.
	frame := decodeOneControl(t, ws.last())
	mustEqual(t, frame["hijacked"], true, "rest hijacked broadcast")
	mustEqual(t, frame["owner"], "other", "rest owner other")
	mustEqual(t, jnumf(frame["lease_expires_at"]), 5100.0, "rest lease wall time")
}

func TestCleanupBrowserDisconnectFacadeError(t *testing.T) {
	h, clk := newTestHub(t, func(c *TermHubConfig) {
		c.ResumeStore = markErrResumeStore{NewInMemoryResumeStore(c.Clock, seqTokenGen())}
	})
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("owner")
	_, _ = h.RegisterBrowser(bg(), "w1", ws, "operator", false)
	st := h.registry.Get("w1")
	st.WorkerWS = worker
	st.HijackOwner = ws
	exp := clk.Monotonic() + 100
	st.HijackOwnerExpiresAt = &exp
	_, err := h.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	if err == nil {
		t.Fatal("expected facade to surface the mark-owner error")
	}
}

func TestRegisterBrowserNonPrincipalConn(t *testing.T) {
	// A browser conn that is not a principalCarrier exercises the !ok branch of
	// browserPrincipalSubjectID (quota exempt).
	h, _ := newTestHub(t, nil)
	bad := &plainConn{id: 7}
	_, err := h.Conn.RegisterBrowser(bg(), "w1", bad, "viewer", false)
	mustEqual(t, err, nil, "non-principal conn registers without quota")
}

func TestSendWorkerCancellationPropagates(t *testing.T) {
	h, _ := newTestHub(t, nil)
	worker := &fakeWorkerWS{failSend: errors.New("cancelled send")}
	registerWorkerState(h, "w1", worker)
	ctx, cancel := context.WithCancel(bg())
	cancel() // ctx.Err() != nil
	ok, err := h.SendWorker(ctx, "w1", map[string]any{"type": "input", "data": "x"})
	mustFalse(t, ok, "cancelled send -> false")
	if err == nil {
		t.Fatal("cancellation should propagate the send error")
	}
}

func TestRedactSnapshotPrepareError(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
		c.IdentityProvider = errIdentityProvider{}
	})
	_, err := h.Router.RedactSnapshotForRecipient(bg(), "w1", map[string]any{"screen": "s"}, newBrowserWS("r"))
	if err == nil {
		t.Fatal("expected prepare-context error to propagate")
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"sync"
	"testing"
)

// recordingSink records telemetry events.
type recordingSink struct {
	mu     sync.Mutex
	events []TelemetryEvent
	err    error
}

func (s *recordingSink) Emit(_ context.Context, evt TelemetryEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, evt)
	return s.err
}

func (s *recordingSink) types() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, len(s.events))
	for i, e := range s.events {
		out[i] = e.EventType
	}
	return out
}

func TestConfigDefaultsAndClamps(t *testing.T) {
	h := NewTermHub(TermHubConfig{Logger: discardLogger()})
	mustEqual(t, h.MaxInputChars(), 10000, "default max input chars")
	mustEqual(t, h.WorkerFrameOnInvalid(), "drop", "default worker frame policy")
	mustEqual(t, h.MaxWSMessageBytes(), 1_048_576, "default ws bytes")

	// Clamps: tiny values are floored.
	h2 := NewTermHub(TermHubConfig{Logger: discardLogger(), MaxWSMessageBytes: 10, MaxInputChars: 5})
	mustEqual(t, h2.MaxWSMessageBytes(), 1024, "ws bytes floored to 1024")
	mustEqual(t, h2.MaxInputChars(), 100, "input chars floored to 100")

	if h.EventBus() != nil {
		t.Fatal("default event bus nil")
	}
	if h.ResumeStore() != nil {
		t.Fatal("default resume store nil")
	}
}

func TestRegisterWorkerEmitsTelemetry(t *testing.T) {
	sink := &recordingSink{}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	_, _ = h.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustDeepEqual(t, sink.types(), []string{"session.registered"}, "worker register telemetry")
}

func TestRegisterWorkerCapacityNoTelemetry(t *testing.T) {
	sink := &recordingSink{}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink; c.MaxWorkers = 1 })
	_, _ = h.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	_, err := h.RegisterWorker(bg(), "w2", &fakeWorkerWS{})
	if err == nil {
		t.Fatal("expected capacity error")
	}
	// Only the successful register emitted telemetry.
	mustEqual(t, len(sink.types()), 1, "no telemetry on rejected register")
}

func TestTelemetryFailOpen(t *testing.T) {
	sink := &recordingSink{err: errors.New("sink down")}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	// A raising sink must not break the operation.
	ok, err := h.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, err, nil, "register succeeds despite sink error")
	mustFalse(t, ok, "new worker not previously hijacked")
}

func TestNilSinkNoTelemetry(t *testing.T) {
	h, _ := newTestHub(t, nil)
	// No sink: emitTelemetry is a no-op (exercises the nil-guard).
	_, _ = h.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
}

func TestDeregisterWorkerFacadeClosesEventBus(t *testing.T) {
	bus := NewEventBus(EventBusOptions{Logger: discardLogger()})
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.EventBus = bus })
	ws := &fakeWorkerWS{}
	registerWorkerState(h, "w1", ws)
	sub, remove, _ := bus.Watch("w1", nil, nil)
	defer remove()
	should, _ := h.DeregisterWorker(bg(), "w1", ws)
	mustTrue(t, should, "should broadcast disconnect")
	select {
	case ev := <-sub.Queue:
		if ev != nil {
			t.Fatal("expected sentinel")
		}
	default:
		t.Fatal("event bus not closed on deregister")
	}
}

func TestSetWorkerHelloModeInvalid(t *testing.T) {
	h, _ := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	_, err := h.SetWorkerHelloMode(bg(), "w1", "bogus")
	var invalid *InvalidInputModeError
	if !errors.As(err, &invalid) {
		t.Fatalf("expected InvalidInputModeError, got %v", err)
	}
	ok, err := h.SetWorkerHelloMode(bg(), "w1", "open")
	mustEqual(t, err, nil, "valid mode ok")
	mustTrue(t, ok, "open applied")
}

func TestLeaseFacadeAcquireRestTelemetry(t *testing.T) {
	sink := &recordingSink{}
	h, clk := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ok, reason, err := h.TryAcquireRestHijack(bg(), "w1", "op-1", 60, "hj-1", clk.Monotonic())
	mustEqual(t, err, nil, "no err")
	mustTrue(t, ok, "rest acquired")
	mustEqual(t, reason, "", "no reason")
	mustDeepEqual(t, sink.types(), []string{"hijack.acquired"}, "acquire telemetry")

	// The REST lease is now valid.
	mustTrue(t, h.CheckHijackValid("w1", "hj-1"), "hijack valid")
	mustTrue(t, h.CheckStillHijacked("w1"), "still hijacked")
}

func TestLeaseFacadeAcquireWsTelemetry(t *testing.T) {
	sink := &recordingSink{}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ws := newBrowserWS("b")
	ok, reason := h.TryAcquireWsHijack(bg(), "w1", ws)
	mustTrue(t, ok, "ws hijack acquired")
	mustEqual(t, reason, "", "no reason")
	mustDeepEqual(t, sink.types(), []string{"hijack.acquired"}, "ws acquire telemetry")

	released, restActive := h.TryReleaseWsHijack(bg(), "w1", ws)
	mustTrue(t, released, "released")
	mustFalse(t, restActive, "no rest lease")
	mustDeepEqual(t, sink.types(), []string{"hijack.acquired", "hijack.released"}, "release telemetry")
}

func TestCleanupExpiredHijackTelemetry(t *testing.T) {
	sink := &recordingSink{}
	h, clk := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w1", worker)
	st.HijackSession = &HijackSession{HijackID: "h", Owner: "op", LeaseExpiresAt: clk.Monotonic() - 1}

	cleaned, err := h.CleanupExpiredHijack(bg(), "w1")
	mustEqual(t, err, nil, "no err")
	mustTrue(t, cleaned, "expired lease cleaned")
	found := false
	for _, tp := range sink.types() {
		if tp == "hijack.expired" {
			found = true
		}
	}
	mustTrue(t, found, "expired telemetry emitted")
}

func TestGetRestSessionCleansThenReturns(t *testing.T) {
	h, clk := newTestHub(t, nil)
	registerWorkerState(h, "w1", &fakeWorkerWS{})
	ok, _, _ := h.TryAcquireRestHijack(bg(), "w1", "op", 60, "hj-1", clk.Monotonic())
	mustTrue(t, ok, "acquired")
	sess, err := h.GetRestSession(bg(), "w1", "hj-1")
	mustEqual(t, err, nil, "no err")
	if sess == nil || sess.HijackID != "hj-1" {
		t.Fatalf("expected active session, got %v", sess)
	}
	// Wrong id -> nil.
	miss, _ := h.GetRestSession(bg(), "w1", "other")
	if miss != nil {
		t.Fatal("wrong hijack id -> nil")
	}
}

func TestWaitForSnapshotViaPolling(t *testing.T) {
	h, clk := newTestHub(t, nil)
	worker := &fakeWorkerWS{}
	st := registerWorkerState(h, "w1", worker)
	// A snapshot with ts newer than the request wall time is already present.
	st.LastSnapshot = map[string]any{"ts": clk.Wall() + 10, "screen": "ready"}
	snap, err := h.WaitForSnapshot(bg(), "w1", 1000)
	mustEqual(t, err, nil, "no err")
	if snap == nil || snap["screen"] != "ready" {
		t.Fatalf("expected snapshot, got %v", snap)
	}
	// The poll requested a fresh snapshot from the worker.
	if worker.last() == "" {
		t.Fatal("expected snapshot_req sent to worker")
	}
}

// ---- Output redaction seam (broadcast + read paths) ------------------------

type staticOutputGate struct {
	rules []RedactionRule
	err   error
}

func (g staticOutputGate) GetRedactionRules(context.Context, PolicyContext) ([]RedactionRule, error) {
	return g.rules, g.err
}

// upperRedactor replaces the "screen" field with a constant marker.
func upperRedactor(msg map[string]any, _ []RedactionRule) map[string]any {
	out := map[string]any{}
	for k, v := range msg {
		out[k] = v
	}
	if _, ok := out["screen"]; ok {
		out["screen"] = "[REDACTED]"
	}
	return out
}

func TestBroadcastAppliesRoleRedaction(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x", Replacement: "y"}}}
		c.Redactor = upperRedactor
	})
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)

	err := h.Broadcast(bg(), "w1", map[string]any{"type": "snapshot", "screen": "secret"})
	mustEqual(t, err, nil, "broadcast err")
	frame := decodeOneControl(t, a.last())
	mustEqual(t, frame["screen"], "[REDACTED]", "screen redacted by role")
}

func TestBroadcastGateInactiveWithoutContentType(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
	})
	a := newBrowserWS("a")
	st := NewWorkerTermState()
	st.Browsers[a] = "viewer"
	h.registry.Put("w1", st)
	// A non-content frame type is not gated.
	_ = h.Broadcast(bg(), "w1", map[string]any{"type": "custom", "screen": "secret"})
	frame := decodeOneControl(t, a.last())
	mustEqual(t, frame["screen"], "secret", "non-content frame not redacted")
}

func TestRedactSnapshotForRecipientNoRules(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: nil}
		c.Redactor = upperRedactor
	})
	snap := map[string]any{"screen": "raw"}
	out, err := h.Router.RedactSnapshotForRecipient(bg(), "w1", snap, newBrowserWS("r"))
	mustEqual(t, err, nil, "no err")
	mustEqual(t, out["screen"], "raw", "no rules -> unchanged")
}

func TestGetLastSnapshotRedacted(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
		c.Redactor = upperRedactor
	})
	st := NewWorkerTermState()
	st.LastSnapshot = map[string]any{"screen": "secret", "type": "snapshot"}
	h.registry.Put("w1", st)
	out, _ := h.GetLastSnapshot(bg(), "w1", newBrowserWS("r"))
	mustEqual(t, out["screen"], "[REDACTED]", "read-path redaction")
	// Stored snapshot not mutated.
	mustEqual(t, st.LastSnapshot["screen"], "secret", "stored snapshot untouched")
}

func TestResolveRoleForBrowserDefault(t *testing.T) {
	h, _ := newTestHub(t, nil)
	role, err := h.ResolveRoleForBrowser(bg(), newBrowserWS("x"), "w1")
	mustEqual(t, err, nil, "no err")
	mustEqual(t, role, "viewer", "default role viewer")
}

func TestIsHijackedPredicate(t *testing.T) {
	h, clk := newTestHub(t, nil)
	st := NewWorkerTermState()
	mustFalse(t, h.IsHijacked(st), "idle not hijacked")
	st.HijackSession = &HijackSession{LeaseExpiresAt: clk.Monotonic() + 10}
	mustTrue(t, h.IsHijacked(st), "rest lease hijacked")
}

func TestCleanupBrowserDisconnectFacadeForgets(t *testing.T) {
	sink := &recordingSink{}
	h, _ := newTestHub(t, func(c *TermHubConfig) { c.TelemetrySink = sink })
	worker := &fakeWorkerWS{}
	ws := newBrowserWS("b")
	registerWorkerState(h, "w1", worker)
	_, _ = h.RegisterBrowser(bg(), "w1", ws, "operator", false)
	h.Router.RecordKeystroke(ws)

	_, err := h.CleanupBrowserDisconnect(bg(), "w1", ws, false)
	mustEqual(t, err, nil, "no err")
	// Heuristic state forgotten.
	mustEqual(t, h.Router.GetHeuristics(ws)["cps"], 0.0, "keystroke state forgotten")
	// disconnected telemetry emitted (register also emitted).
	found := false
	for _, tp := range sink.types() {
		if tp == "session.disconnected" {
			found = true
		}
	}
	mustTrue(t, found, "disconnect telemetry emitted")
}

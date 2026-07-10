//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"context"
	"encoding/base64"
	"testing"
	"time"
)

func TestNewInterceptGateClamps(t *testing.T) {
	g := NewInterceptGate(0.1, "explode")
	if g.TimeoutS() != 1.0 {
		t.Fatalf("timeout should clamp to 1.0, got %v", g.TimeoutS())
	}
	if g.TimeoutAction() != "forward" {
		t.Fatalf("invalid action should coerce to forward, got %q", g.TimeoutAction())
	}
	if !g.InspectEnabled() || g.Enabled() {
		t.Fatal("defaults: inspect on, intercept off")
	}
	g2 := NewInterceptGate(5, "drop")
	if g2.TimeoutAction() != "drop" {
		t.Fatalf("drop action should survive, got %q", g2.TimeoutAction())
	}
}

func TestParseActionForwardDefault(t *testing.T) {
	if d := ParseActionMessage(map[string]any{}); d.Action != "forward" {
		t.Fatalf("missing action should default forward, got %q", d.Action)
	}
	if d := ParseActionMessage(map[string]any{"action": "bogus"}); d.Action != "forward" {
		t.Fatalf("unknown action should coerce forward, got %q", d.Action)
	}
	if d := ParseActionMessage(map[string]any{"action": "drop"}); d.Action != "drop" || d.Headers != nil || d.Body != nil {
		t.Fatalf("drop decision unexpected: %+v", d)
	}
}

func TestParseActionModifySanitizes(t *testing.T) {
	body := []byte("new-body")
	d := ParseActionMessage(map[string]any{
		"action": "modify",
		"headers": map[string]any{
			"X-Custom":      "keep",
			"Host":          "evil.example",
			"Authorization": "Bearer x",
			"Count":         float64(3),
			"Flag":          true,
		},
		"body_b64": base64.StdEncoding.EncodeToString(body),
	})
	if d.Action != "modify" {
		t.Fatalf("action = %q", d.Action)
	}
	if d.Headers["X-Custom"] != "keep" {
		t.Fatalf("custom header dropped: %v", d.Headers)
	}
	if _, ok := d.Headers["Host"]; ok {
		t.Fatal("Host must be denylisted")
	}
	if _, ok := d.Headers["Authorization"]; ok {
		t.Fatal("Authorization must be denylisted")
	}
	if d.Headers["Count"] != "3" {
		t.Fatalf("numeric header coercion = %q", d.Headers["Count"])
	}
	if d.Headers["Flag"] != "True" {
		t.Fatalf("bool header coercion = %q", d.Headers["Flag"])
	}
	if string(d.Body) != string(body) {
		t.Fatalf("body = %q", d.Body)
	}
}

func TestParseActionModifyBadBase64(t *testing.T) {
	d := ParseActionMessage(map[string]any{"action": "modify", "body_b64": "!!!not base64!!!"})
	if d.Body != nil {
		t.Fatalf("invalid base64 body should be ignored, got %q", d.Body)
	}
}

func TestSanitizeHeadersDropped(t *testing.T) {
	_, dropped := SanitizeHeaders(map[string]string{"Cookie": "a", "Connection": "keep-alive", "Ok": "1"})
	if len(dropped) != 2 {
		t.Fatalf("expected 2 dropped, got %v", dropped)
	}
	// sorted
	if dropped[0] != "Connection" || dropped[1] != "Cookie" {
		t.Fatalf("dropped not sorted: %v", dropped)
	}
}

func TestGateResolve(t *testing.T) {
	g := NewInterceptGate(5, "forward")
	done := make(chan InterceptDecision, 1)
	go func() { done <- g.AwaitDecision(context.Background(), "r1") }()
	// wait until pending registers
	waitFor(t, func() bool { return g.PendingCount() == 1 })
	if !g.Resolve("r1", InterceptDecision{Action: "drop"}) {
		t.Fatal("resolve should find r1")
	}
	if d := <-done; d.Action != "drop" {
		t.Fatalf("awaited decision = %q", d.Action)
	}
	if g.Resolve("r1", InterceptDecision{Action: "forward"}) {
		t.Fatal("second resolve should fail")
	}
	if g.PendingCount() != 0 {
		t.Fatalf("pending should be empty, got %d", g.PendingCount())
	}
}

func TestGateTimeout(t *testing.T) {
	g := NewInterceptGate(1, "drop") // clamped to 1s min
	// Override to a short wait by using ctx cancel instead (timeout path is 1s).
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if d := g.AwaitDecision(ctx, "r1"); d.Action != "drop" {
		t.Fatalf("cancelled await should return timeout action drop, got %q", d.Action)
	}
}

func TestGateTimeoutFires(t *testing.T) {
	g := NewInterceptGate(1, "forward")
	start := time.Now()
	d := g.AwaitDecision(context.Background(), "r1")
	if d.Action != "forward" {
		t.Fatalf("timeout decision = %q", d.Action)
	}
	if time.Since(start) < 900*time.Millisecond {
		t.Fatalf("timeout fired too early: %v", time.Since(start))
	}
}

func TestGateCancelAll(t *testing.T) {
	g := NewInterceptGate(5, "forward")
	results := make(chan InterceptDecision, 2)
	for _, rid := range []string{"a", "b"} {
		go func(id string) { results <- g.AwaitDecision(context.Background(), id) }(rid)
	}
	waitFor(t, func() bool { return g.PendingCount() == 2 })
	if n := g.CancelAll("drop"); n != 2 {
		t.Fatalf("cancel-all resolved %d, want 2", n)
	}
	for i := 0; i < 2; i++ {
		if d := <-results; d.Action != "drop" {
			t.Fatalf("cancel-all decision = %q", d.Action)
		}
	}
}

func TestGateToggles(t *testing.T) {
	g := NewInterceptGate(5, "forward")
	g.SetEnabled(true)
	if !g.Enabled() {
		t.Fatal("SetEnabled(true) failed")
	}
	g.SetInspectEnabled(false)
	if g.InspectEnabled() {
		t.Fatal("SetInspectEnabled(false) failed")
	}
}

func TestStringifyHeaderValue(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{"plain", "plain"},
		{float64(7), "7"},
		{1.5, "1.5"},
		{true, "True"},
		{false, "False"},
		{[]any{1, 2}, "[1 2]"},
	}
	for _, c := range cases {
		if got := stringifyHeaderValue(c.in); got != c.want {
			t.Fatalf("stringifyHeaderValue(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition not met in time")
}

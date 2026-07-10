//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"strings"
	"testing"
)

// goodIDs / badIDs mirror the Python path-injection test corpus.
var goodIDs = []string{"worker-1", "wh1", "s1", "session.1", "a_b-C.3", "UUID-1234-5678", "x"}
var badIDs = []string{"../../api/keys", "..", ".", "a/b", "w%2fx", "", "wo rker", "a\\b", "with/slash", "tab\tid"}

func TestSafeIDAcceptsGood(t *testing.T) {
	for _, v := range goodIDs {
		got, err := safeID(v, "id")
		if err != nil || got != v {
			t.Fatalf("safeID(%q) = %q, %v", v, got, err)
		}
	}
}

func TestSafeIDRejectsBad(t *testing.T) {
	for _, v := range badIDs {
		_, err := safeID(v, "worker_id")
		if err == nil {
			t.Fatalf("safeID(%q) accepted; want error", v)
		}
		if !strings.Contains(err.Error(), "invalid") || !strings.Contains(err.Error(), "worker_id") {
			t.Fatalf("safeID(%q) error = %q", v, err)
		}
	}
}

func TestPathBuildersValidate(t *testing.T) {
	c := NewHijackClient("http://test")

	if p, err := c.wp("worker-1"); err != nil || p != "/worker/worker-1" {
		t.Fatalf("wp: %q %v", p, err)
	}
	if p, err := c.hp("worker-1", "hj-2"); err != nil || p != "/worker/worker-1/hijack/hj-2" {
		t.Fatalf("hp: %q %v", p, err)
	}
	if p, err := c.sp("s1"); err != nil || p != "/api/sessions/s1" {
		t.Fatalf("sp: %q %v", p, err)
	}

	if _, err := c.wp("../../api/keys"); err == nil || !strings.Contains(err.Error(), "worker_id") {
		t.Fatalf("wp traversal: %v", err)
	}
	if _, err := c.hp("a/b", "hj"); err == nil || !strings.Contains(err.Error(), "worker_id") {
		t.Fatalf("hp bad worker: %v", err)
	}
	if _, err := c.hp("worker-1", "../x"); err == nil || !strings.Contains(err.Error(), "hijack_id") {
		t.Fatalf("hp bad hijack: %v", err)
	}
	if _, err := c.sp("../keys"); err == nil || !strings.Contains(err.Error(), "session_id") {
		t.Fatalf("sp traversal: %v", err)
	}
}

// TestPublicMethodsRejectInjectedIDs asserts each path-family method fails
// closed before issuing any HTTP request (no server is running).
func TestPublicMethodsRejectInjectedIDs(t *testing.T) {
	c := NewHijackClient("http://test")

	if _, err := c.Acquire(ctx(), "../../api/keys", AcquireOptions{}); err == nil ||
		!strings.Contains(err.Error(), "worker_id") {
		t.Fatalf("acquire injection: %v", err)
	}
	if _, err := c.Snapshot(ctx(), "worker-1", "../../api/keys", 0); err == nil ||
		!strings.Contains(err.Error(), "hijack_id") {
		t.Fatalf("snapshot injection: %v", err)
	}
	if _, err := c.GetSession(ctx(), "../../api/keys"); err == nil ||
		!strings.Contains(err.Error(), "session_id") {
		t.Fatalf("get_session injection: %v", err)
	}
	// Every id-bearing method funnels through wp/hp/sp; exercise the rest so
	// each early-return branch is covered.
	if _, err := c.Heartbeat(ctx(), "a/b", "hj", 0); err == nil {
		t.Fatal("heartbeat injection")
	}
	if _, err := c.Send(ctx(), "a/b", "hj", SendOptions{Keys: "x"}); err == nil {
		t.Fatal("send injection")
	}
	if _, err := c.Step(ctx(), "a/b", "hj"); err == nil {
		t.Fatal("step injection")
	}
	if _, err := c.Release(ctx(), "a/b", "hj"); err == nil {
		t.Fatal("release injection")
	}
	if _, err := c.Events(ctx(), "a/b", "hj", EventsOptions{}); err == nil {
		t.Fatal("events injection")
	}
	if _, err := c.SetInputMode(ctx(), "a/b", "open"); err == nil {
		t.Fatal("set_input_mode injection")
	}
	if _, err := c.DisconnectWorker(ctx(), "a/b"); err == nil {
		t.Fatal("disconnect_worker injection")
	}
	if _, err := c.SessionSnapshot(ctx(), "../k"); err == nil {
		t.Fatal("session_snapshot injection")
	}
	if _, err := c.SessionEvents(ctx(), "../k", 0); err == nil {
		t.Fatal("session_events injection")
	}
	if _, err := c.WatchSessionEvents(ctx(), "../k", WatchOptions{}); err == nil {
		t.Fatal("watch injection")
	}
	if _, err := c.SetSessionMode(ctx(), "../k", "open"); err == nil {
		t.Fatal("set_session_mode injection")
	}
	if _, err := c.ConnectSession(ctx(), "../k"); err == nil {
		t.Fatal("connect_session injection")
	}
	if _, err := c.DisconnectSession(ctx(), "../k"); err == nil {
		t.Fatal("disconnect_session injection")
	}
}

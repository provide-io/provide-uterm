//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

func newCtrl(h Hub) *Controller {
	return NewController(h, Config{Clock: hub.NewManualClock(1234.5), IDGen: func() string { return "sid" }})
}

// -- Group CRUD --------------------------------------------------------------

func TestCreateGroupAndList(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	gid, err := ctrl.CreateGroup(newGroup(t, []string{"w1"}, nil), "admin")
	if err != nil || gid != "g1" {
		t.Fatalf("CreateGroup = %q, %v", gid, err)
	}
	groups := ctrl.ListGroups("admin")
	if len(groups) != 1 || groups[0].GroupID != "g1" {
		t.Fatalf("ListGroups = %+v", groups)
	}
}

func TestCreateGroupEnforcesMaxSize(t *testing.T) {
	ctrl := NewController(newFakeHub(nil), Config{MaxGroupSize: 2})
	_, err := ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, nil), "admin")
	if err == nil || !strings.Contains(err.Error(), "exceeds max") {
		t.Fatalf("want exceeds-max error, got %v", err)
	}
}

func TestCreateGroupErrorPatternValidation(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	// Oversized pattern.
	_, err := ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) {
		g.ErrorPattern = strings.Repeat("a", maxErrorPatternLen+1)
	}), "admin")
	if err == nil || !strings.Contains(err.Error(), "too long") {
		t.Fatalf("want too-long error, got %v", err)
	}
	// Invalid pattern.
	_, err = ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) { g.ErrorPattern = "(unterminated" }), "admin")
	if err == nil || !strings.Contains(err.Error(), "invalid") {
		t.Fatalf("want invalid error, got %v", err)
	}
	// Valid pattern.
	if _, err := ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) { g.ErrorPattern = "ERROR:" }), "admin"); err != nil {
		t.Fatalf("valid pattern rejected: %v", err)
	}
}

func TestDeleteGroup(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1"}, nil), "admin")
	ctrl.DeleteGroup("g1", "admin")
	if ctrl.GetGroup("g1", "admin") != nil {
		t.Fatal("group should be gone")
	}
	// Delete of a missing/unauthorized group is a silent no-op.
	ctrl.DeleteGroup("nonexistent", "admin")
}

func TestGrantAccess(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) { g.CreatedBy = "admin" }), "admin")

	if len(ctrl.ListGroups("bob")) != 0 {
		t.Fatal("bob should see nothing before grant")
	}
	ctrl.GrantAccess("g1", "bob", "admin")
	if len(ctrl.ListGroups("bob")) != 1 {
		t.Fatal("bob should see group after grant")
	}
	// Re-granting the same principal is a no-op (no duplicate).
	ctrl.GrantAccess("g1", "bob", "admin")
	g := ctrl.GetGroup("g1", "admin")
	count := 0
	for _, x := range g.Grants {
		if x == "bob" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("bob granted %d times, want 1", count)
	}
	// Non-creator cannot grant.
	ctrl.GrantAccess("g1", "carol", "bob")
	if contains(ctrl.GetGroup("g1", "admin").Grants, "carol") {
		t.Fatal("non-creator grant must be rejected")
	}
	// Grant on a missing group is a no-op.
	ctrl.GrantAccess("missing", "x", "admin")
}

func TestAuthorizedGroupOwnership(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) { g.CreatedBy = "alice" }), "alice")
	if ctrl.GetGroup("g1", "alice") == nil {
		t.Fatal("creator should see group")
	}
	if ctrl.GetGroup("g1", "bob") != nil {
		t.Fatal("non-owner should get nil")
	}
	ctrl.GrantAccess("g1", "bob", "alice")
	if ctrl.GetGroup("g1", "bob") == nil {
		t.Fatal("grantee should see group")
	}
}

// -- Send --------------------------------------------------------------------

func TestSendGroupNotFound(t *testing.T) {
	ctrl := newCtrl(newFakeHub(nil))
	r := ctrl.Send(context.Background(), "nonexistent", "ls\n", "admin", 0, 0)
	if r.GroupID != "nonexistent" || len(r.Results) != 0 || len(r.FailedSessions) != 0 || len(r.DivergentSessions) != 0 {
		t.Fatalf("empty result expected, got %+v", r)
	}
	if r.SendID != "sid" || r.Command != "ls\n" || r.SentAt != 1234.5 {
		t.Fatalf("result metadata = %+v", r)
	}
	// No workers should have been contacted.
	if len(newFakeHub(nil).sendCalls()) != 0 {
		t.Fatal("sanity")
	}
}

func TestSendUnauthorizedPrincipal(t *testing.T) {
	fh := newFakeHub(nil, "w1")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1"}, func(g *Group) { g.CreatedBy = "alice" }), "alice")
	r := ctrl.Send(context.Background(), "g1", "cmd\n", "bob", 0, 0)
	if len(r.Results) != 0 {
		t.Fatalf("unauthorized send must be empty, got %+v", r)
	}
	if len(fh.sendCalls()) != 0 {
		t.Fatal("unauthorized send must not reach any worker")
	}
}

func TestSendParallelAllConnected(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w2", "w3")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, nil), "admin")

	for _, w := range []string{"w1", "w2", "w3"} {
		go waitAndEmitTerm(bus, w, "ok\n")
	}
	r := ctrl.Send(context.Background(), "g1", "ls\n", "admin", 0, 0)

	if r.GroupID != "g1" || r.Command != "ls\n" || len(r.Results) != 3 {
		t.Fatalf("result = %+v", r)
	}
	for _, sr := range r.Results {
		if !sr.OK || derefStr(sr.OutputDelta) != "ok\n" {
			t.Fatalf("session result = %+v", sr)
		}
	}
	if len(r.FailedSessions) != 0 {
		t.Fatalf("failed = %v", r.FailedSessions)
	}
	// Each worker received the input frame.
	byWorker := map[string]map[string]any{}
	for _, c := range fh.sendCalls() {
		byWorker[c.WorkerID] = c.Msg
	}
	for _, w := range []string{"w1", "w2", "w3"} {
		msg := byWorker[w]
		if msg == nil || msg["type"] != "input" || msg["data"] != "ls\n" {
			t.Fatalf("worker %s input frame = %+v", w, msg)
		}
	}
}

func TestSendParallelPartialFailure(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1") // only w1 connected
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, nil), "admin")

	go waitAndEmitTerm(bus, "w1", "output from w1")
	r := ctrl.Send(context.Background(), "g1", "cmd\n", "admin", 0, 0)

	if len(r.Results) != 3 {
		t.Fatalf("want 3 results, got %d", len(r.Results))
	}
	if !r.Results[0].OK || derefStr(r.Results[0].OutputDelta) != "output from w1" {
		t.Fatalf("w1 result = %+v", r.Results[0])
	}
	if r.Results[1].OK || r.Results[2].OK {
		t.Fatalf("w2/w3 should be failed: %+v", r.Results)
	}
	failed := map[string]bool{}
	for _, w := range r.FailedSessions {
		failed[w] = true
	}
	if !failed["w2"] || !failed["w3"] || len(r.FailedSessions) != 2 {
		t.Fatalf("failed sessions = %v", r.FailedSessions)
	}
}

func TestSendParallelDivergence(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w2", "w3")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, func(g *Group) { g.DivergenceThreshold = 0.9 }), "admin")

	go waitAndEmitTerm(bus, "w1", "hello world")
	go waitAndEmitTerm(bus, "w2", "hello world")
	go waitAndEmitTerm(bus, "w3", "completely different text xyz")
	r := ctrl.Send(context.Background(), "g1", "cmd\n", "admin", 0, 0)

	if len(r.DivergentSessions) != 1 || r.DivergentSessions[0] != "w3" {
		t.Fatalf("divergent = %v, want [w3]", r.DivergentSessions)
	}
	for _, sr := range r.Results {
		if sr.WorkerID == "w3" && !sr.Divergent {
			t.Fatal("w3 should be flagged divergent")
		}
		if sr.WorkerID != "w3" && sr.Divergent {
			t.Fatalf("%s should not be divergent", sr.WorkerID)
		}
	}
}

func TestSendSequentialInOrder(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w2", "w3")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, func(g *Group) { g.Mode = "sequential" }), "admin")

	// Sequential collects w1 fully before sending w2, so emit per worker as its
	// subscription appears.
	for _, w := range []string{"w1", "w2", "w3"} {
		w := w
		go waitAndEmitTerm(bus, w, "output-"+w)
	}
	r := ctrl.Send(context.Background(), "g1", "cmd\n", "admin", 0, 0)

	order := []string{}
	for _, c := range fh.sendCalls() {
		order = append(order, c.WorkerID)
	}
	if strings.Join(order, ",") != "w1,w2,w3" {
		t.Fatalf("send order = %v, want w1,w2,w3", order)
	}
	if len(r.Results) != 3 {
		t.Fatalf("results = %d", len(r.Results))
	}
	for i, w := range []string{"w1", "w2", "w3"} {
		if !r.Results[i].OK || derefStr(r.Results[i].OutputDelta) != "output-"+w {
			t.Fatalf("result[%d] = %+v", i, r.Results[i])
		}
	}
}

func TestSendSequentialStopOnFirstError(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w2", "w3")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, func(g *Group) {
		g.Mode = "sequential"
		g.StopOnFirstError = true
		g.ErrorPattern = "ERROR:"
	}), "admin")

	go waitAndEmitTerm(bus, "w1", "success")
	go waitAndEmitTerm(bus, "w2", "ERROR: something failed")
	// w3 must NOT be contacted because processing stops after w2's error.
	r := ctrl.Send(context.Background(), "g1", "deploy\n", "admin", 0, 0)

	if !r.Results[0].OK || r.Results[0].WorkerID != "w1" {
		t.Fatalf("w1 = %+v", r.Results[0])
	}
	if !r.Results[1].OK || !strings.Contains(derefStr(r.Results[1].OutputDelta), "ERROR:") {
		t.Fatalf("w2 = %+v", r.Results[1])
	}
	if r.Results[2].OK || r.Results[2].WorkerID != "w3" {
		t.Fatalf("w3 should be failed/skipped: %+v", r.Results[2])
	}
	if !contains(r.FailedSessions, "w3") {
		t.Fatalf("w3 should be in failed_sessions: %v", r.FailedSessions)
	}
	// w3 was never sent to.
	for _, c := range fh.sendCalls() {
		if c.WorkerID == "w3" {
			t.Fatal("w3 must not receive input after stop")
		}
	}
}

func TestSendSequentialPartialFailure(t *testing.T) {
	// w1 + w3 connected, w2 disconnected. Sequential must mark w2 failed and
	// still proceed to w3 (no stop-on-error configured).
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w3")
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2", "w3"}, func(g *Group) { g.Mode = "sequential" }), "admin")

	go waitAndEmitTerm(bus, "w1", "out1")
	go waitAndEmitTerm(bus, "w3", "out3")
	r := ctrl.Send(context.Background(), "g1", "cmd\n", "admin", 0, 0)

	if !r.Results[0].OK || r.Results[1].OK || !r.Results[2].OK {
		t.Fatalf("results ok = %v,%v,%v want true,false,true",
			r.Results[0].OK, r.Results[1].OK, r.Results[2].OK)
	}
	if !contains(r.FailedSessions, "w2") || len(r.FailedSessions) != 1 {
		t.Fatalf("failed = %v, want [w2]", r.FailedSessions)
	}
	if derefStr(r.Results[2].OutputDelta) != "out3" {
		t.Fatalf("w3 delta = %q", derefStr(r.Results[2].OutputDelta))
	}
}

func TestSendNotifiesObservers(t *testing.T) {
	// No workers connected + nil bus: send is empty of results but still
	// broadcasts fanout_input to every target's observers.
	fh := newFakeHub(nil)
	ctrl := newCtrl(fh)
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"wa", "wb"}, func(g *Group) { g.CreatedBy = "alice" }), "alice")

	ctrl.Send(context.Background(), "g1", "uptime\n", "alice", 0, 0)

	got := map[string]map[string]any{}
	for _, c := range fh.broadcastCalls() {
		got[c.WorkerID] = c.Msg
	}
	if len(got) != 2 {
		t.Fatalf("broadcasts to %d workers, want 2", len(got))
	}
	for _, w := range []string{"wa", "wb"} {
		f := got[w]
		if f == nil || f["type"] != "fanout_input" || f["from_principal"] != "alice" ||
			f["command"] != "uptime\n" || f["group_id"] != "g1" {
			t.Fatalf("worker %s fanout_input frame = %+v", w, f)
		}
	}
}

func TestNewControllerDefaults(t *testing.T) {
	// nil Store/Clock/IDGen must select working defaults.
	ctrl := NewController(newFakeHub(nil), Config{})
	gid, err := ctrl.CreateGroup(newGroup(t, []string{"w1"}, nil), "admin")
	if err != nil || gid == "" {
		t.Fatalf("default controller CreateGroup = %q, %v", gid, err)
	}
	r := ctrl.Send(context.Background(), "g1", "x", "admin", 0, 0)
	if len(r.SendID) != 32 { // default newHexID → 32 hex chars
		t.Fatalf("default SendID len = %d (%q), want 32", len(r.SendID), r.SendID)
	}
}

func TestValidateErrorPatternEmpty(t *testing.T) {
	re, err := validateErrorPattern("")
	if re != nil || err != nil {
		t.Fatalf("empty pattern = %v, %v", re, err)
	}
}

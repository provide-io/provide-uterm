//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

type fakeAuthorizer struct {
	mu      sync.Mutex
	admin   bool
	denied  map[string]bool
	checked []string
}

func allowAllAuthorizer() *fakeAuthorizer { return &fakeAuthorizer{admin: true} }

func (a *fakeAuthorizer) IsGlobalAdmin(p *serverauth.Principal) bool {
	return a != nil && a.admin && p != nil && p.Roles.Has("admin") && p.AdminSessionScope == nil
}

func (a *fakeAuthorizer) CanReadMember(_ context.Context, _ *serverauth.Principal, workerID string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.checked = append(a.checked, workerID)
	return !a.denied[workerID]
}

func (a *fakeAuthorizer) checkedMembers() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]string(nil), a.checked...)
}

func adminPrincipal(subject string) *serverauth.Principal {
	return &serverauth.Principal{
		SubjectID: subject,
		Roles:     serverauth.NewSet("admin"),
		Scopes:    serverauth.NewSet("*"),
	}
}

// sendCall records one SendWorker invocation.
type sendCall struct {
	WorkerID string
	Msg      map[string]any
}

// bcastCall records one Broadcast invocation.
type bcastCall struct {
	WorkerID string
	Msg      map[string]any
}

// fakeHub is a concurrency-safe [Hub] for controller tests. It records every
// SendWorker + Broadcast call, returns true from SendWorker only for the
// workers in connected, and exposes a real EventBus so the collector runs.
type fakeHub struct {
	mu         sync.Mutex
	bus        *hub.EventBus
	connected  map[string]bool
	sends      []sendCall
	broadcasts []bcastCall
	onSend     func(string)
}

func newFakeHub(bus *hub.EventBus, connected ...string) *fakeHub {
	set := map[string]bool{}
	for _, w := range connected {
		set[w] = true
	}
	return &fakeHub{bus: bus, connected: set}
}

func (f *fakeHub) SendWorker(_ context.Context, workerID string, msg map[string]any) (bool, error) {
	f.mu.Lock()
	f.sends = append(f.sends, sendCall{WorkerID: workerID, Msg: msg})
	connected := f.connected[workerID]
	onSend := f.onSend
	f.mu.Unlock()
	if onSend != nil {
		onSend(workerID)
	}
	return connected, nil
}

func (f *fakeHub) Broadcast(_ context.Context, workerID string, msg map[string]any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.broadcasts = append(f.broadcasts, bcastCall{WorkerID: workerID, Msg: msg})
	return nil
}

func (f *fakeHub) EventBus() *hub.EventBus { return f.bus }

func (f *fakeHub) sendCalls() []sendCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]sendCall, len(f.sends))
	copy(out, f.sends)
	return out
}

func (f *fakeHub) broadcastCalls() []bcastCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]bcastCall, len(f.broadcasts))
	copy(out, f.broadcasts)
	return out
}

// waitAndEmitTerm blocks until workerID has a live EventBus subscriber (the
// collector), then enqueues each chunk as a term event. It gives up after 2s so
// a wiring bug fails the test rather than hanging.
func waitAndEmitTerm(bus *hub.EventBus, workerID string, chunks ...string) {
	deadline := time.Now().Add(2 * time.Second)
	for bus.SubscriberCount(workerID) < 1 {
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(time.Millisecond)
	}
	for _, ch := range chunks {
		bus.Enqueue(workerID, map[string]any{"type": "term", "data": map[string]any{"data": ch}})
	}
}

// newGroup builds a test group with short quiesce so tests run fast.
func newGroup(t *testing.T, workerIDs []string, mutate func(g *Group)) *Group {
	t.Helper()
	g := &Group{
		GroupID:             "g1",
		Name:                "test-group",
		WorkerIDs:           workerIDs,
		CreatedBy:           "admin",
		CreatedAt:           1.0,
		Mode:                "parallel",
		QuiesceMS:           40,
		MaxResponseMS:       5000,
		DivergenceThreshold: 0.8,
	}
	if mutate != nil {
		mutate(g)
	}
	return g
}

// derefStr returns *p or "<nil>" for readable assertions.
func derefStr(p *string) string {
	if p == nil {
		return "<nil>"
	}
	return *p
}

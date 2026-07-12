// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package server

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

type graphicalLifecycleEngine struct {
	cp.Engine
	close func(context.Context) error
	begin func(context.Context) (cp.Tx, error)
}

func (e *graphicalLifecycleEngine) Close(ctx context.Context) error { return e.close(ctx) }
func (e *graphicalLifecycleEngine) Begin(ctx context.Context) (cp.Tx, error) {
	if e.begin != nil {
		return e.begin(ctx)
	}
	return e.Engine.Begin(ctx)
}

func graphicalTarget(id string, tenant *string) serverconfig.GraphicalTargetDefinition {
	return serverconfig.GraphicalTargetDefinition{TargetID: id, Endpoint: "dns:///" + id + ".example:443", TLSMode: "tls", AllowedVMPatterns: []string{"*"}, TenantID: tenant, MinimumRole: "viewer", ConnectTimeoutS: 10, HandshakeTimeoutS: 10, ReadTimeoutS: 30, WriteTimeoutS: 30, ShutdownTimeoutS: 5, MaxGRPCMessageBytes: 16 << 20, MaxFramebufferWidth: 8192, MaxFramebufferHeight: 8192, MaxRectangles: 4096, MaxClipboardBytes: 1 << 20, MaxPixelAllocationBytes: 256 << 20}
}

func TestGraphicalRegistryMergeScopeCRUDAndStaticPrecedence(t *testing.T) {
	ctx := context.Background()
	engine := memory.New(cp.Config{})
	if err := engine.Open(ctx); err != nil {
		t.Fatal(err)
	}
	one := "one"
	static := graphicalTarget("static", &one)
	registry, err := NewGraphicalTargetRegistry([]serverconfig.GraphicalTargetDefinition{static}, engine, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = registry.Close(context.Background()) })
	tenant, _ := NewTenantTargetScope("one")
	other, _ := NewTenantTargetScope("two")
	system := SystemTargetScope()
	runtime := graphicalTarget("runtime", &one)
	if _, err = registry.Create(ctx, tenant, runtime); err != nil {
		t.Fatal(err)
	}
	got, err := registry.List(ctx, system)
	if err != nil || len(got) != 2 || got[0].TargetID != "runtime" || got[1].TargetID != "static" {
		t.Fatalf("merge: %#v %v", got, err)
	}
	if got, _ := registry.Get(ctx, other, "runtime"); got != nil {
		t.Fatal("cross tenant read")
	}
	if _, err := registry.Create(ctx, system, static); !errors.Is(err, ErrGraphicalTargetAlreadyExists) {
		t.Fatalf("static create: %v", err)
	}
	if _, err := registry.Update(ctx, system, static); !errors.Is(err, ErrGraphicalTargetImmutable) {
		t.Fatalf("static update: %v", err)
	}
	if err := registry.Delete(ctx, system, "static"); !errors.Is(err, ErrGraphicalTargetImmutable) {
		t.Fatalf("static delete: %v", err)
	}
}

func TestGraphicalRegistryCopiesValuesAndCloses(t *testing.T) {
	ctx := context.Background()
	engine := memory.New(cp.Config{})
	_ = engine.Open(ctx)
	target := graphicalTarget("safe", nil)
	registry, err := NewGraphicalTargetRegistry([]serverconfig.GraphicalTargetDefinition{target}, engine, false)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := registry.Get(ctx, SystemTargetScope(), "safe")
	got.AllowedVMPatterns[0] = "mutated"
	again, _ := registry.Get(ctx, SystemTargetScope(), "safe")
	if again.AllowedVMPatterns[0] != "*" {
		t.Fatal("static value alias")
	}
	if err := registry.Close(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := registry.List(ctx, SystemTargetScope()); !errors.Is(err, ErrGraphicalTargetClosed) {
		t.Fatalf("post close: %v", err)
	}
}

func TestGraphicalRegistryCloseFailurePublishesToWaitersAndLaterRetries(t *testing.T) {
	base := memory.New(cp.Config{})
	_ = base.Open(context.Background())
	started := make(chan struct{})
	release := make(chan struct{})
	failure := errors.New("close failed")
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base}
	engine.close = func(context.Context) error {
		n := calls.Add(1)
		if n == 1 {
			close(started)
			<-release
			return failure
		}
		return nil
	}
	r, err := NewGraphicalTargetRegistry(nil, engine, true)
	if err != nil {
		t.Fatal(err)
	}
	results := make(chan error, 2)
	go func() { results <- r.Close(context.Background()) }()
	<-started
	go func() { results <- r.Close(context.Background()) }()
	for {
		r.mu.Lock()
		waiters := r.closeAttempt.waiters
		r.mu.Unlock()
		if waiters == 2 {
			break
		}
	}
	close(release)
	for range 2 {
		if err := <-results; !errors.Is(err, failure) {
			t.Fatalf("shared close failure = %v", err)
		}
	}
	if calls.Load() != 1 {
		t.Fatalf("close calls after shared attempt = %d", calls.Load())
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatalf("retry close: %v", err)
	}
	if calls.Load() != 2 {
		t.Fatalf("retry calls = %d", calls.Load())
	}
}

func TestGraphicalRegistryCloseDrainsActiveOperationAndRejectsNewOperations(t *testing.T) {
	base := memory.New(cp.Config{})
	_ = base.Open(context.Background())
	began := make(chan struct{})
	release := make(chan struct{})
	closed := make(chan struct{})
	engine := &graphicalLifecycleEngine{Engine: base}
	engine.begin = func(ctx context.Context) (cp.Tx, error) { close(began); <-release; return base.Begin(ctx) }
	engine.close = func(context.Context) error { close(closed); return nil }
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	opDone := make(chan error, 1)
	go func() { _, e := r.List(context.Background(), SystemTargetScope()); opDone <- e }()
	<-began
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	for {
		r.mu.Lock()
		closing := r.closing
		r.mu.Unlock()
		if closing {
			break
		}
	}
	if _, err := r.List(context.Background(), SystemTargetScope()); !errors.Is(err, ErrGraphicalTargetClosed) {
		t.Fatalf("operation during close = %v", err)
	}
	select {
	case <-closed:
		t.Fatal("engine closed before active operation drained")
	default:
	}
	close(release)
	if err := <-opDone; err != nil {
		t.Fatal(err)
	}
	if err := <-closeDone; err != nil {
		t.Fatal(err)
	}
}

func TestGraphicalRegistryCanceledCloseCallerDoesNotStrandAttempt(t *testing.T) {
	base := memory.New(cp.Config{})
	_ = base.Open(context.Background())
	started := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, close: func(context.Context) error { calls.Add(1); close(started); <-release; return nil }}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { first <- r.Close(ctx) }()
	<-started
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled leader = %v", err)
	}
	waiter := make(chan error, 1)
	go func() { waiter <- r.Close(context.Background()) }()
	close(release)
	if err := <-waiter; err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatalf("close calls = %d", calls.Load())
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestGraphicalRegistryCanceledLeaderAfterEngineCloseBeforePublication(t *testing.T) {
	base := memory.New(cp.Config{})
	started, release, returned := make(chan struct{}), make(chan struct{}), make(chan struct{})
	engine := &graphicalLifecycleEngine{Engine: base, close: func(context.Context) error {
		close(started)
		<-release
		close(returned)
		return nil
	}}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() { result <- r.Close(ctx) }()
	<-started
	r.mu.Lock()
	close(release)
	<-returned
	cancel()
	r.mu.Unlock()
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled before publication = %v", err)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatalf("published close outcome = %v", err)
	}
}

func TestGraphicalRegistryActiveLeaseReleasedAfterPanic(t *testing.T) {
	base := memory.New(cp.Config{})
	var closeCalls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, begin: func(context.Context) (cp.Tx, error) { panic("begin panic") }, close: func(context.Context) error { closeCalls.Add(1); return nil }}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	func() {
		defer func() {
			if recover() == nil {
				t.Fatal("expected panic")
			}
		}()
		_, _ = r.List(context.Background(), SystemTargetScope())
	}()
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if closeCalls.Load() != 1 {
		t.Fatalf("close calls = %d", closeCalls.Load())
	}
}

func TestGraphicalRegistryConcurrentBorrowedCloseDoesNotCloseEngine(t *testing.T) {
	base := memory.New(cp.Config{})
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, close: func(context.Context) error { calls.Add(1); return nil }}
	r, _ := NewGraphicalTargetRegistry(nil, engine, false)
	var wg sync.WaitGroup
	wg.Add(8)
	for range 8 {
		go func() {
			defer wg.Done()
			if err := r.Close(context.Background()); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 0 {
		t.Fatalf("borrowed close calls = %d", calls.Load())
	}
}

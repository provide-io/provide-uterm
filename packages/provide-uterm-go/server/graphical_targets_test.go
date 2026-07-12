// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package server

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

type graphicalLifecycleEngine struct {
	cp.Engine
	close func(context.Context) error
	begin func(context.Context) (cp.Tx, error)
}

type closeWaiterContext struct {
	context.Context
	reached chan struct{}
	once    sync.Once
}

func (c *closeWaiterContext) Done() <-chan struct{} {
	c.once.Do(func() { close(c.reached) })
	return c.Context.Done()
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

func TestGraphicalRegistryRejectsAndRedactsCorruptPersistedRecords(t *testing.T) {
	for _, backend := range []string{"memory", "sqlite"} {
		t.Run(backend, func(t *testing.T) {
			ctx := context.Background()
			var engine cp.Engine
			if backend == "memory" {
				engine = memory.New(cp.Config{})
			} else {
				engine = sqlite.New(cp.Config{DatabaseURL: t.TempDir() + "/targets.db"})
			}
			if err := engine.Open(ctx); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = engine.Close(context.Background()) })
			if err := engine.Migrate(ctx); err != nil {
				t.Fatal(err)
			}
			payloads := []string{"dns:///sensitive-invalid-endpoint", "sensitive-tls", "sensitive-role", "10.0.0.1/8", "literal-sensitive-secret"}
			records := make([]cp.GraphicalTargetRecord, 6)
			for i := range records {
				records[i] = toGraphicalRecord(graphicalTarget(fmt.Sprintf("corrupt-%d", i), nil), 1)
			}
			records[0].Endpoint = payloads[0]
			records[1].TLSMode = payloads[1]
			records[2].MinimumRole = payloads[2]
			records[3].ConnectTimeoutS = 0
			records[4].AllowedCIDRs = cp.NewStringTuple(payloads[3])
			records[5].CASecretRef = cp.Str(payloads[4])
			tx, _ := engine.Begin(ctx)
			for _, rec := range records {
				if err := engine.GraphicalTargetStore(tx).Put(ctx, rec); err != nil {
					t.Fatal(err)
				}
			}
			if err := tx.Commit(ctx); err != nil {
				t.Fatal(err)
			}
			r, _ := NewGraphicalTargetRegistry(nil, engine, false)
			assertRedacted := func(err error) {
				t.Helper()
				if !errors.Is(err, ErrGraphicalTargetPersistedData) {
					t.Fatalf("error = %v", err)
				}
				for _, payload := range payloads {
					if strings.Contains(err.Error(), payload) {
						t.Fatalf("persisted payload leaked: %v", err)
					}
				}
			}
			for i := range records {
				_, err := r.Get(ctx, SystemTargetScope(), fmt.Sprintf("corrupt-%d", i))
				if err == nil {
					t.Fatalf("corrupt record %d was accepted", i)
				}
				assertRedacted(err)
			}
			_, err := r.List(ctx, SystemTargetScope())
			assertRedacted(err)
			_, err = r.RuntimeRecord(ctx, SystemTargetScope(), "corrupt-0")
			assertRedacted(err)
		})
	}
}

func TestGraphicalRegistryValidationDoesNotNormalizeCallerSlices(t *testing.T) {
	base := memory.New(cp.Config{})
	_ = base.Open(context.Background())
	patterns := []string{"*", "*"}
	cidrs := []string{"10.0.0.0/8", "10.0.0.0/8"}
	target := graphicalTarget("copy", nil)
	target.AllowedVMPatterns = patterns
	target.AllowedCIDRs = cidrs
	r, err := NewGraphicalTargetRegistry([]serverconfig.GraphicalTargetDefinition{target}, base, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(patterns) != 2 || len(cidrs) != 2 {
		t.Fatal("constructor normalization mutated caller slices")
	}
	target.TargetID = "runtime"
	if _, err = r.Create(context.Background(), SystemTargetScope(), target); err != nil {
		t.Fatal(err)
	}
	if len(patterns) != 2 || len(cidrs) != 2 {
		t.Fatal("create normalization mutated caller slices")
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
	waiterCtx := &closeWaiterContext{Context: context.Background(), reached: make(chan struct{})}
	go func() { results <- r.Close(waiterCtx) }()
	<-waiterCtx.reached
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

func TestGraphicalRegistryRetryCloseRedrainsOperationsStartedAfterFailure(t *testing.T) {
	base := memory.New(cp.Config{})
	_ = base.Open(context.Background())
	failure := errors.New("first close failed")
	operationBegan := make(chan struct{})
	releaseOperation := make(chan struct{})
	secondCloseCalled := make(chan struct{})
	var closeCalls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base}
	engine.close = func(context.Context) error {
		if closeCalls.Add(1) == 1 {
			return failure
		}
		close(secondCloseCalled)
		return nil
	}
	engine.begin = func(ctx context.Context) (cp.Tx, error) {
		close(operationBegan)
		<-releaseOperation
		return base.Begin(ctx)
	}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	if err := r.Close(context.Background()); !errors.Is(err, failure) {
		t.Fatalf("first registry close = %v", err)
	}
	opDone := make(chan error, 1)
	go func() { _, err := r.List(context.Background(), SystemTargetScope()); opDone <- err }()
	<-operationBegan
	retryDone := make(chan error, 1)
	go func() { retryDone <- r.Close(context.Background()) }()
	for {
		r.mu.Lock()
		closing := r.closing
		attempt := r.closeAttempt
		r.mu.Unlock()
		if closing && attempt != nil {
			select {
			case <-attempt.drain:
				t.Fatal("retry drain opened while a reopened operation was active")
			default:
			}
			break
		}
	}
	select {
	case <-secondCloseCalled:
		t.Fatal("retry closed engine before reopened operation drained")
	default:
	}
	if _, err := r.List(context.Background(), SystemTargetScope()); !errors.Is(err, ErrGraphicalTargetClosed) {
		t.Fatalf("new operation during retry = %v", err)
	}
	close(releaseOperation)
	if err := <-opDone; err != nil {
		t.Fatal(err)
	}
	if err := <-retryDone; err != nil {
		t.Fatal(err)
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
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, close: func(ctx context.Context) error {
		if calls.Add(1) == 1 {
			close(started)
			<-ctx.Done()
			return ctx.Err()
		}
		return nil
	}}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { first <- r.Close(ctx) }()
	<-started
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled leader = %v", err)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 {
		t.Fatalf("close calls = %d", calls.Load())
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestGraphicalRegistryLeaderContextCancelsEngineCloseAndAllowsRetry(t *testing.T) {
	base := memory.New(cp.Config{})
	started := make(chan struct{})
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, close: func(ctx context.Context) error {
		if calls.Add(1) == 1 {
			close(started)
			<-ctx.Done()
			return ctx.Err()
		}
		return nil
	}}
	r, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Close(ctx) }()
	<-started
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled close = %v", err)
	}
	if err := r.Close(context.Background()); err != nil {
		t.Fatalf("retry = %v", err)
	}
	if calls.Load() != 2 {
		t.Fatalf("close calls = %d", calls.Load())
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
	if err := <-result; err != nil {
		t.Fatalf("successful engine close outcome = %v", err)
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

func TestServerShutdownPropagatesGraphicalRegistryCloseFailure(t *testing.T) {
	failure := errors.New("graphical close failure")
	base := memory.New(cp.Config{})
	var calls atomic.Int32
	engine := &graphicalLifecycleEngine{Engine: base, close: func(context.Context) error {
		if calls.Add(1) == 1 {
			return failure
		}
		return nil
	}}
	registry, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	if err := ts.srv.Shutdown(); !errors.Is(err, failure) {
		t.Fatalf("Shutdown error = %v", err)
	}
	if err := ts.srv.Shutdown(); err != nil {
		t.Fatalf("Shutdown retry = %v", err)
	}
}

func TestJoinShutdownErrorsPreservesHTTPAndGraphicalFailures(t *testing.T) {
	httpErr := errors.New("http shutdown")
	graphicalErr := errors.New("graphical shutdown")
	joined := joinShutdownErrors(httpErr, graphicalErr)
	if !errors.Is(joined, httpErr) || !errors.Is(joined, graphicalErr) {
		t.Fatalf("joined error = %v", joined)
	}
}

func TestServerShutdownJoinsHTTPAndGraphicalFailures(t *testing.T) {
	httpErr := errors.New("http failure")
	graphicalErr := errors.New("graphical failure")
	base := memory.New(cp.Config{})
	engine := &graphicalLifecycleEngine{Engine: base, close: func(context.Context) error { return graphicalErr }}
	registry, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	ts.srv.shutdownHTTP = func(context.Context) error { return httpErr }
	err := ts.srv.Shutdown()
	if !errors.Is(err, httpErr) || !errors.Is(err, graphicalErr) {
		t.Fatalf("Shutdown error = %v", err)
	}
}

func TestServerGraphicalShutdownUsesBoundedContext(t *testing.T) {
	base := memory.New(cp.Config{})
	observed := make(chan time.Duration, 1)
	engine := &graphicalLifecycleEngine{Engine: base, close: func(ctx context.Context) error {
		deadline, ok := ctx.Deadline()
		if !ok {
			return errors.New("missing deadline")
		}
		observed <- time.Until(deadline)
		return nil
	}}
	registry, _ := NewGraphicalTargetRegistry(nil, engine, true)
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) { deps.GraphicalTargets = registry })
	if err := ts.srv.Shutdown(); err != nil {
		t.Fatal(err)
	}
	if d := <-observed; d <= 0 || d > 11*time.Second {
		t.Fatalf("shutdown deadline = %v", d)
	}
}

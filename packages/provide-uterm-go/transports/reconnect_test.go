//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

func listFactory(stubs ...*stubTransport) (TransportFactory, *int) {
	i := 0
	idx := &i
	return func() ConnectionTransport {
		s := stubs[*idx]
		*idx++
		return s
	}, idx
}

func recordingSleep() (SleepFunc, *[]time.Duration) {
	var calls []time.Duration
	return func(_ context.Context, d time.Duration) error {
		calls = append(calls, d)
		return nil
	}, &calls
}

func TestPolicyDelayBounded(t *testing.T) {
	p := ReconnectPolicy{MaxRetries: 3, BaseBackoff: 500 * time.Millisecond, MaxBackoff: time.Second}
	got := []time.Duration{p.policyDelay(0), p.policyDelay(1), p.policyDelay(2), p.policyDelay(3)}
	want := []time.Duration{500 * time.Millisecond, 500 * time.Millisecond, time.Second, time.Second}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("policyDelay(%d) = %v, want %v", i, got[i], want[i])
		}
	}
}

func TestConnectWithRetriesBackoffPattern(t *testing.T) {
	// Mirror of Python test_connect_with_retries_backoff_is_bounded.
	s1, s2, s3 := newStubTransport(), newStubTransport(), newStubTransport()
	s1.connectErr = errors.New("unavailable")
	s2.connectErr = errors.New("unavailable")
	s3.connectErr = errors.New("unavailable")
	ok := newStubTransport()
	factory, _ := listFactory(s1, s2, s3, ok)
	sleep, calls := recordingSleep()

	policy := ReconnectPolicy{MaxRetries: 3, BaseBackoff: 500 * time.Millisecond, MaxBackoff: time.Second}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	if err := rt.Connect(context.Background(), "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	want := []time.Duration{500 * time.Millisecond, time.Second, time.Second}
	if len(*calls) != len(want) {
		t.Fatalf("sleep calls = %v, want %v", *calls, want)
	}
	for i := range want {
		if (*calls)[i] != want[i] {
			t.Errorf("sleep[%d] = %v, want %v", i, (*calls)[i], want[i])
		}
	}
	if rt.Inner() != ok {
		t.Error("expected inner to be the successful stub")
	}
}

func TestConnectRetriesExhausted(t *testing.T) {
	s1, s2 := newStubTransport(), newStubTransport()
	s1.connectErr = errors.New("nope")
	s2.connectErr = errors.New("nope")
	factory, _ := listFactory(s1, s2)
	sleep, _ := recordingSleep()
	policy := ReconnectPolicy{MaxRetries: 1, BaseBackoff: 0}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	err := rt.Connect(context.Background(), "h", 1, ConnectOptions{})
	if !errors.Is(err, ErrRetriesExhausted) {
		t.Errorf("want ErrRetriesExhausted, got %v", err)
	}
}

func TestSendReconnectsAndCallsHook(t *testing.T) {
	failing := newStubTransport()
	failing.sendErr = ErrConnectionClosed
	recovered := newStubTransport()
	factory, _ := listFactory(failing, recovered)
	sleep, _ := recordingSleep()

	var hookCalls int
	var hookArg ConnectionTransport
	rt := NewReconnectingTransport(factory, ReconnectingOptions{
		Sleep:       sleep,
		OnReconnect: func(tr ConnectionTransport) { hookCalls++; hookArg = tr },
	})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("A")); err != nil {
		t.Fatalf("send: %v", err)
	}
	if hookCalls != 1 || hookArg != recovered {
		t.Errorf("hook calls=%d arg=%v", hookCalls, hookArg)
	}
	if failing.disconnectN == 0 {
		t.Error("failing transport should have been disconnected")
	}
	if len(recovered.sent) != 1 || string(recovered.sent[0]) != "A" {
		t.Errorf("recovered.sent = %v", recovered.sent)
	}
}

func TestSendReconnectExhausted(t *testing.T) {
	s1 := newStubTransport()
	s1.sendErr = ErrConnectionClosed
	s2 := newStubTransport()
	s2.sendErr = ErrConnectionClosed
	factory, _ := listFactory(s1, s2)
	sleep, _ := recordingSleep()
	policy := ReconnectPolicy{MaxRetries: 1, BaseBackoff: 0}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("x")); !errors.Is(err, ErrRetriesExhausted) {
		t.Errorf("want ErrRetriesExhausted, got %v", err)
	}
}

func TestNonRetryableErrorNotReconnected(t *testing.T) {
	s1 := newStubTransport()
	logicErr := errors.New("not a transport failure")
	s1.sendErr = logicErr
	factory, _ := listFactory(s1)
	rt := NewReconnectingTransport(factory, ReconnectingOptions{})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("x")); !errors.Is(err, logicErr) {
		t.Errorf("want logic error, got %v", err)
	}
	if s1.disconnectN != 0 {
		t.Error("should not reconnect on logic error")
	}
}

func TestReceiveReconnectAndDelegate(t *testing.T) {
	failing := newStubTransport()
	failing.receiveErr = ErrConnectionClosed
	recovered := newStubTransport([]byte("hello"))
	factory, _ := listFactory(failing, recovered)
	sleep, _ := recordingSleep()
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	got, err := rt.Receive(ctx, 128, 10*time.Millisecond)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if string(got) != "hello" {
		t.Errorf("received %q", got)
	}
}

func TestReconnectBackoffSkippedWhenZeroDelay(t *testing.T) {
	// BaseBackoff > 0 but MaxBackoff 0 -> computed delay 0 -> no sleep.
	failing := newStubTransport()
	failing.sendErr = ErrConnectionClosed
	recovered := newStubTransport()
	factory, _ := listFactory(failing, recovered)
	sleep, calls := recordingSleep()
	policy := ReconnectPolicy{MaxRetries: 2, BaseBackoff: 500 * time.Millisecond, MaxBackoff: 0}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("x")); err != nil {
		t.Fatalf("send: %v", err)
	}
	if len(*calls) != 0 {
		t.Errorf("expected no sleeps, got %v", *calls)
	}
}

func TestReconnectingTransportLifecycle(t *testing.T) {
	s := newStubTransport()
	factory, _ := listFactory(s)
	rt := NewReconnectingTransport(factory, ReconnectingOptions{})
	ctx := context.Background()

	// Not connected yet: IsConnected false, Send returns not-connected.
	if rt.IsConnected() {
		t.Error("should not be connected before Connect")
	}
	if err := rt.Send(ctx, []byte("x")); !errors.Is(err, ErrNotConnected) {
		t.Errorf("send before connect: %v", err)
	}
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if !rt.IsConnected() {
		t.Error("should be connected")
	}
	if err := rt.Disconnect(ctx); err != nil {
		t.Fatalf("disconnect: %v", err)
	}
	if rt.IsConnected() {
		t.Error("should be disconnected")
	}
	// Disconnect again is a no-op.
	if err := rt.Disconnect(ctx); err != nil {
		t.Errorf("second disconnect: %v", err)
	}
}

func TestConnectWithRetriesSleepError(t *testing.T) {
	s1, s2 := newStubTransport(), newStubTransport()
	s1.connectErr = errors.New("nope")
	s2.connectErr = errors.New("nope")
	factory, _ := listFactory(s1, s2)
	sleepErr := errors.New("ctx cancelled")
	sleep := func(_ context.Context, _ time.Duration) error { return sleepErr }
	policy := ReconnectPolicy{MaxRetries: 3, BaseBackoff: time.Second, MaxBackoff: time.Second}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	if err := rt.Connect(context.Background(), "h", 1, ConnectOptions{}); !errors.Is(err, sleepErr) {
		t.Errorf("want sleep error, got %v", err)
	}
}

func TestReconnectBackoffSleepError(t *testing.T) {
	failing := newStubTransport()
	failing.sendErr = ErrConnectionClosed
	recovered := newStubTransport()
	factory, _ := listFactory(failing, recovered)
	sleepErr := errors.New("cancelled during backoff")
	sleep := func(_ context.Context, _ time.Duration) error { return sleepErr }
	policy := ReconnectPolicy{MaxRetries: 2, BaseBackoff: time.Second, MaxBackoff: time.Second}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("x")); !errors.Is(err, sleepErr) {
		t.Errorf("want sleep error from reconnect backoff, got %v", err)
	}
}

func TestReconnectInnerConnectFails(t *testing.T) {
	// Send fails (retryable) -> reconnect -> new factory transport fails to
	// connect -> connectWithRetries exhausts -> error propagates from reconnect.
	failing := newStubTransport()
	failing.sendErr = ErrConnectionClosed
	broken := newStubTransport()
	broken.connectErr = errors.New("dial failed")
	broken2 := newStubTransport()
	broken2.connectErr = errors.New("dial failed")
	factory, _ := listFactory(failing, broken, broken2)
	sleep, _ := recordingSleep()
	policy := ReconnectPolicy{MaxRetries: 1, BaseBackoff: 0}
	rt := NewReconnectingTransport(factory, ReconnectingOptions{Policy: &policy, Sleep: sleep})
	ctx := context.Background()
	if err := rt.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := rt.Send(ctx, []byte("x")); !errors.Is(err, ErrRetriesExhausted) {
		t.Errorf("want retries exhausted from failed reconnect, got %v", err)
	}
}

func TestDefaultReconnectPolicyTracksDefaults(t *testing.T) {
	p := DefaultReconnectPolicy()
	if p.MaxRetries != defaults.ReconnectMaxRetries {
		t.Errorf("MaxRetries = %d, want %d", p.MaxRetries, defaults.ReconnectMaxRetries)
	}
	if p.BaseBackoff != secondsToDuration(defaults.ReconnectBaseBackoffS) {
		t.Errorf("BaseBackoff = %v", p.BaseBackoff)
	}
	if p.MaxBackoff != secondsToDuration(defaults.ReconnectMaxBackoffS) {
		t.Errorf("MaxBackoff = %v", p.MaxBackoff)
	}
}

func TestDefaultIsRetryable(t *testing.T) {
	if defaultIsRetryable(nil) {
		t.Error("nil should not be retryable")
	}
	if !defaultIsRetryable(ErrConnectionClosed) {
		t.Error("ErrConnectionClosed should be retryable")
	}
	if !defaultIsRetryable(ErrNotConnected) {
		t.Error("ErrNotConnected should be retryable")
	}
	if defaultIsRetryable(errors.New("plain")) {
		t.Error("plain error should not be retryable")
	}
	// A net.Error wrapped error is retryable.
	if !defaultIsRetryable(&net_timeoutError{}) {
		t.Error("net timeout should be retryable")
	}
}

// net_timeoutError is a minimal net.Error for the retryable test.
type net_timeoutError struct{}

func (net_timeoutError) Error() string   { return "i/o timeout" }
func (net_timeoutError) Timeout() bool   { return true }
func (net_timeoutError) Temporary() bool { return true }

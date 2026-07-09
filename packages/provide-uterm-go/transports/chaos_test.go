//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// stubTransport is an in-memory ConnectionTransport for wrapper tests. It is a
// port of the Python StubTransport test double.
type stubTransport struct {
	mu          sync.Mutex
	connected   bool
	sent        [][]byte
	responses   [][]byte
	rxIndex     int
	connectErr  error
	sendErr     error
	receiveErr  error
	connectN    int
	disconnectN int
}

func newStubTransport(responses ...[]byte) *stubTransport {
	return &stubTransport{connected: true, responses: responses}
}

func (s *stubTransport) Connect(_ context.Context, _ string, _ int, _ ConnectOptions) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.connectN++
	if s.connectErr != nil {
		return s.connectErr
	}
	s.connected = true
	return nil
}

func (s *stubTransport) Disconnect(_ context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.disconnectN++
	s.connected = false
	return nil
}

func (s *stubTransport) Send(_ context.Context, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sendErr != nil {
		return s.sendErr
	}
	s.sent = append(s.sent, append([]byte(nil), data...))
	return nil
}

func (s *stubTransport) Receive(_ context.Context, _ int, _ time.Duration) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.receiveErr != nil {
		return nil, s.receiveErr
	}
	if s.rxIndex < len(s.responses) {
		r := s.responses[s.rxIndex]
		s.rxIndex++
		return r, nil
	}
	return []byte{}, nil
}

func (s *stubTransport) IsConnected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connected
}

func TestChaosPassthrough(t *testing.T) {
	ctx := context.Background()
	inner := newStubTransport([]byte("data1"), []byte("data2"))
	c := NewChaosTransport(inner, ChaosConfig{})

	if got, _ := c.Receive(ctx, 128, 100*time.Millisecond); string(got) != "data1" {
		t.Errorf("recv1 = %q", got)
	}
	if got, _ := c.Receive(ctx, 128, 100*time.Millisecond); string(got) != "data2" {
		t.Errorf("recv2 = %q", got)
	}
	if err := c.Send(ctx, []byte("hello")); err != nil {
		t.Fatalf("send: %v", err)
	}
	if len(inner.sent) != 1 || string(inner.sent[0]) != "hello" {
		t.Errorf("sent = %v", inner.sent)
	}
	if !c.IsConnected() {
		t.Error("want connected")
	}
	inner.connected = false
	if c.IsConnected() {
		t.Error("want disconnected")
	}
}

func TestChaosConnectDisconnectDelegate(t *testing.T) {
	ctx := context.Background()
	inner := newStubTransport()
	inner.connected = false
	c := NewChaosTransport(inner, ChaosConfig{})
	if err := c.Connect(ctx, "h", 1, ConnectOptions{}); err != nil {
		t.Fatalf("connect: %v", err)
	}
	if !inner.connected {
		t.Error("inner should be connected")
	}
	if err := c.Disconnect(ctx); err != nil {
		t.Fatalf("disconnect: %v", err)
	}
	if inner.connected {
		t.Error("inner should be disconnected")
	}
}

func TestChaosDisconnectInjection(t *testing.T) {
	ctx := context.Background()
	inner := newStubTransport()
	inner.responses = [][]byte{[]byte("ok"), []byte("ok"), []byte("ok"), []byte("ok"), []byte("ok")}
	c := NewChaosTransport(inner, ChaosConfig{Seed: 1, DisconnectEveryNReceives: 3})

	var injected error
	for i := 0; i < 5; i++ {
		if _, err := c.Receive(ctx, 128, 100*time.Millisecond); err != nil {
			injected = err
			break
		}
	}
	if injected == nil || !strings.Contains(injected.Error(), "injected disconnect") {
		t.Errorf("want injected disconnect, got %v", injected)
	}
	if inner.disconnectN == 0 {
		t.Error("inner disconnect should have been called")
	}
}

func TestChaosTimeoutInjection(t *testing.T) {
	ctx := context.Background()
	inner := newStubTransport([]byte("data"), []byte("data"))
	c := NewChaosTransport(inner, ChaosConfig{Seed: 1, TimeoutEveryNReceives: 2})
	if _, err := c.Receive(ctx, 128, time.Millisecond); err != nil {
		t.Fatalf("recv1: %v", err)
	}
	got, err := c.Receive(ctx, 128, time.Millisecond)
	if err != nil {
		t.Fatalf("recv2: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("injected timeout should return empty, got %v", got)
	}
}

func TestChaosTimeoutInjectionZeroTimeout(t *testing.T) {
	// timeout=0 -> sleepCtx early-returns nil (d<=0 branch), still returns empty.
	ctx := context.Background()
	inner := newStubTransport([]byte("data"), []byte("data"))
	c := NewChaosTransport(inner, ChaosConfig{Seed: 1, TimeoutEveryNReceives: 1})
	got, err := c.Receive(ctx, 128, 0)
	if err != nil {
		t.Fatalf("recv: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("want empty on zero-timeout injection, got %v", got)
	}
}

func TestChaosJitterPreservesData(t *testing.T) {
	ctx := context.Background()
	inner := newStubTransport([]byte("abc"))
	c := NewChaosTransport(inner, ChaosConfig{Seed: 42, MaxJitterMs: 2})
	got, err := c.Receive(ctx, 128, 100*time.Millisecond)
	if err != nil || string(got) != "abc" {
		t.Errorf("jitter corrupted data: %q err=%v", got, err)
	}
}

func TestChaosJitterContextCancel(t *testing.T) {
	// A cancelled context aborts the jitter sleep (seed 42 yields jitter > 0).
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	inner := newStubTransport([]byte("abc"))
	c := NewChaosTransport(inner, ChaosConfig{Seed: 42, MaxJitterMs: 1000})
	if _, err := c.Receive(ctx, 128, time.Second); !errors.Is(err, context.Canceled) {
		t.Errorf("want context canceled from jitter, got %v", err)
	}
}

func TestChaosTimeoutInjectionContextCancel(t *testing.T) {
	// A cancelled context aborts the injected-timeout sleep deterministically.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	inner := newStubTransport([]byte("abc"))
	c := NewChaosTransport(inner, ChaosConfig{Seed: 7, TimeoutEveryNReceives: 1})
	if _, err := c.Receive(ctx, 128, time.Second); !errors.Is(err, context.Canceled) {
		t.Errorf("want context canceled, got %v", err)
	}
}

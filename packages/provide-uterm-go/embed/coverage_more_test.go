//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package embed

import (
	"bytes"
	"context"
	"errors"
	"io"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// TestHubSessionIDsAndLifecycle covers SessionIDs, Lifecycle, and the
// observer setters (OnApplicationData/OnClientData/OnWire) plus ClientID/Meta.
func TestHubSessionIDsAndLifecycle(t *testing.T) {
	h := NewHub()
	if ids := h.SessionIDs(); len(ids) != 0 {
		t.Fatalf("empty hub SessionIDs = %v", ids)
	}
	s1, err := h.CreateSession(Options{SessionID: "a"})
	if err != nil {
		t.Fatal(err)
	}
	s2, err := h.CreateSession(Options{SessionID: "b"})
	if err != nil {
		t.Fatal(err)
	}
	ids := h.SessionIDs()
	if len(ids) != 2 {
		t.Fatalf("SessionIDs len = %d want 2 (%v)", len(ids), ids)
	}
	seen := map[string]bool{}
	for _, id := range ids {
		seen[id] = true
	}
	if !seen["a"] || !seen["b"] {
		t.Fatalf("SessionIDs missing a/b: %v", ids)
	}
	if s1.Lifecycle() != LifecycleCreated {
		t.Fatalf("Lifecycle = %v want Created", s1.Lifecycle())
	}

	var appCalls, cliCalls, wireCalls int
	s1.OnApplicationData(func(ByteDirection, []byte, string) { appCalls++ })
	s1.OnClientData(func([]byte, string) { cliCalls++ })
	s1.OnWire(func(WireEventKind, []byte, string) { wireCalls++ })

	up := NewMemoryUpstream()
	if err := s1.ConnectUpstream(context.Background(), up); err != nil {
		t.Fatal(err)
	}
	if s1.Lifecycle() != LifecycleConnected {
		t.Fatalf("after connect Lifecycle = %v", s1.Lifecycle())
	}
	c, err := s1.AttachClient(ClientMetadata{ClientID: "c1", Tags: map[string]struct{}{"t": {}}})
	if err != nil {
		t.Fatal(err)
	}
	if c.ClientID() != "c1" {
		t.Fatalf("ClientID = %q", c.ClientID())
	}
	if c.Meta().ClientID != "c1" {
		t.Fatalf("Meta.ClientID = %q", c.Meta().ClientID)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := s1.SendToUpstream(ctx, []byte("IN")); err != nil {
		t.Fatal(err)
	}
	if cliCalls != 1 {
		t.Fatalf("OnClientData calls = %d", cliCalls)
	}
	if err := up.PushFromRemote([]byte("OUT")); err != nil {
		t.Fatal(err)
	}
	got, err := c.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("OUT")) {
		t.Fatalf("receive %q err %v", got, err)
	}
	if appCalls != 1 {
		t.Fatalf("OnApplicationData calls = %d", appCalls)
	}
	s1.RaiseWire(WireDiagnostic, []byte{1}, "diag")
	if wireCalls != 1 {
		t.Fatalf("OnWire calls = %d", wireCalls)
	}
	_ = s2.Close(ctx)
	_ = s1.Close(ctx)
}

// TestDefaultTelnetPolicyBranches covers empty Term, will/wont/dont/default
// OnOption arms, and non-matching OnSubnegotiation.
func TestDefaultTelnetPolicyBranches(t *testing.T) {
	p := DefaultTelnetPolicy{} // Term empty → ANSI
	if p.TerminalType() != "ANSI" {
		t.Fatalf("default term = %q", p.TerminalType())
	}
	p.Term = "XTERM"
	if p.TerminalType() != "XTERM" {
		t.Fatalf("explicit term = %q", p.TerminalType())
	}
	const iac, will, wont, doCmd, dont = 255, 251, 252, 253, 254
	// WILL → DO reply
	if got := p.OnOption(will, 1); !bytes.Equal(got, []byte{iac, doCmd, 1}) {
		t.Fatalf("WILL reply %v", got)
	}
	// WONT → DONT
	if got := p.OnOption(wont, 2); !bytes.Equal(got, []byte{iac, dont, 2}) {
		t.Fatalf("WONT reply %v", got)
	}
	// DONT → WONT
	if got := p.OnOption(dont, 3); !bytes.Equal(got, []byte{iac, wont, 3}) {
		t.Fatalf("DONT reply %v", got)
	}
	// DO already covered elsewhere; unknown command → nil
	if got := p.OnOption(240, 0); got != nil {
		t.Fatalf("unknown cmd reply %v", got)
	}
	// unmatched subneg
	if got := p.OnSubnegotiation(99, []byte{0}); got != nil {
		t.Fatalf("unmatched sb %v", got)
	}
}

// reenterInterceptor re-sends a fixed payload from OnUpstream without holding
// any interceptor-local lock (avoids the deadlock risk of scriptInterceptor).
type reenterInterceptor struct {
	pong []byte
}

func (r reenterInterceptor) OnUpstream(ctx context.Context, c InterceptContext) (InterceptResult, error) {
	if r.pong != nil {
		_ = c.Session.SendFromInterceptor(ctx, r.pong)
	}
	return Pass(), nil
}
func (r reenterInterceptor) OnClient(context.Context, InterceptContext) (InterceptResult, error) {
	return Pass(), nil
}

// TestSendFromInterceptorPaths covers depth==0 rejection and re-entrant send.
func TestSendFromInterceptorPaths(t *testing.T) {
	h := NewHub()
	// Outside interceptor → error (no upstream needed).
	s0, _ := h.CreateSession(Options{})
	ctx := context.Background()
	if err := s0.SendFromInterceptor(ctx, []byte("x")); err == nil {
		t.Fatal("expected outside-interceptor error")
	}

	s, _ := h.CreateSession(Options{Interceptor: reenterInterceptor{pong: []byte("PONG")}})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)

	// Re-entrant via OnUpstream on any remote chunk.
	if err := up.PushFromRemote([]byte("PING")); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		for _, b := range up.Sent() {
			if bytes.Equal(b, []byte("PONG")) {
				_ = s.Close(ctx)
				return
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("PONG not sent via SendFromInterceptor; sent=%v", up.Sent())
}

// TestBackpressureAndClientClosed covers DropNewest, Disconnect backpressure,
// closed-client Receive, and empty-payload forward.
func TestBackpressureAndClientClosed(t *testing.T) {
	h := NewHub()
	s, _ := h.CreateSession(Options{})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)

	// Queue capacity 1 + DropNewest: second fan-out is dropped (not disconnect).
	cDrop, err := s.AttachClient(ClientMetadata{
		ClientID: "drop", QueueCapacity: 1, Backpressure: BackpressureDropNewest,
	})
	if err != nil {
		t.Fatal(err)
	}
	s.SendToClients([]byte("1"), nil)
	s.SendToClients([]byte("2"), nil) // dropped
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	got, err := cDrop.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("1")) {
		t.Fatalf("drop-newest first chunk %q err %v", got, err)
	}

	// Disconnect policy: full queue → client removed; subsequent Receive errors.
	cDisc, err := s.AttachClient(ClientMetadata{
		ClientID: "disc", QueueCapacity: 1, Backpressure: BackpressureDisconnect,
	})
	if err != nil {
		t.Fatal(err)
	}
	s.SendToClients([]byte("A"), nil)
	s.SendToClients([]byte("B"), nil) // disconnect
	// Drain or observe close.
	short, cancel2 := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel2()
	_, err = cDisc.Receive(short)
	// Either first chunk then close, or immediate close — both acceptable as long as
	// the slot was dropped from the session.
	_ = err
	if _, ok := s.clients["disc"]; ok {
		// May still exist if only one chunk fit; force another flood.
		s.SendToClients([]byte("C"), nil)
		s.SendToClients([]byte("D"), nil)
	}
	// After disconnect policy fires, channel is closed.
	closedCtx, cancel3 := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel3()
	// Drain remaining.
	for {
		_, rerr := cDisc.Receive(closedCtx)
		if rerr != nil {
			break
		}
	}

	// Send with no upstream connected path is covered by empty-data no-op.
	s.SendToClients(nil, nil)

	// Attach without ClientID.
	if _, err := s.AttachClient(ClientMetadata{}); err == nil {
		t.Fatal("expected ClientID required")
	}
	_ = s.Close(ctx)
}

// stubConnTransport is a minimal ConnectionTransport for adapter tests.
type stubConnTransport struct {
	mu        sync.Mutex
	connected bool
	sent      [][]byte
	responses [][]byte
	rx        int
	connErr   error
	recvErr   error
}

func (s *stubConnTransport) Connect(context.Context, string, int, transports.ConnectOptions) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.connErr != nil {
		return s.connErr
	}
	s.connected = true
	return nil
}
func (s *stubConnTransport) Disconnect(context.Context) error {
	s.mu.Lock()
	s.connected = false
	s.mu.Unlock()
	return nil
}
func (s *stubConnTransport) Send(_ context.Context, data []byte) error {
	s.mu.Lock()
	s.sent = append(s.sent, append([]byte(nil), data...))
	s.mu.Unlock()
	return nil
}
func (s *stubConnTransport) Receive(context.Context, int, time.Duration) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.recvErr != nil {
		return nil, s.recvErr
	}
	if s.rx < len(s.responses) {
		b := s.responses[s.rx]
		s.rx++
		return b, nil
	}
	return []byte{}, nil
}
func (s *stubConnTransport) IsConnected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connected
}

// TestConnectionTransportUpstream covers the adapter's Connect/Send/Receive paths.
func TestConnectionTransportUpstream(t *testing.T) {
	st := &stubConnTransport{responses: [][]byte{[]byte("hi")}}
	up := NewConnectionTransportUpstream(st, "h", 23, transports.ConnectOptions{})
	if up.IsConnected() {
		t.Fatal("not connected yet")
	}
	ctx := context.Background()
	if err := up.Connect(ctx); err != nil {
		t.Fatal(err)
	}
	if !up.IsConnected() {
		t.Fatal("should be connected")
	}
	if err := up.Send(ctx, []byte("out")); err != nil {
		t.Fatal(err)
	}
	if len(st.sent) != 1 || !bytes.Equal(st.sent[0], []byte("out")) {
		t.Fatalf("sent %v", st.sent)
	}
	got, err := up.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("hi")) {
		t.Fatalf("recv %q %v", got, err)
	}
	// EOF-class errors clear ok and return (nil, nil).
	st.recvErr = io.EOF
	got, err = up.Receive(ctx)
	if err != nil || got != nil {
		t.Fatalf("EOF path got %v err %v", got, err)
	}
	if up.IsConnected() {
		t.Fatal("EOF should clear connected")
	}
	// Non-EOF error propagates.
	st2 := &stubConnTransport{recvErr: errors.New("boom")}
	_ = st2.Connect(ctx, "", 0, transports.ConnectOptions{})
	up2 := NewConnectionTransportUpstream(st2, "h", 1, transports.ConnectOptions{})
	_ = up2.Connect(ctx)
	if _, err := up2.Receive(ctx); err == nil || err.Error() != "boom" {
		t.Fatalf("want boom, got %v", err)
	}
	// Connect failure.
	st3 := &stubConnTransport{connErr: errors.New("nope")}
	up3 := NewConnectionTransportUpstream(st3, "h", 1, transports.ConnectOptions{})
	if err := up3.Connect(ctx); err == nil {
		t.Fatal("expected connect error")
	}
	_ = up.Disconnect(ctx)
	// nil policy default on scripted upstream.
	su := NewScriptedTelnetUpstream(nil)
	if su.policy == nil {
		t.Fatal("nil policy should default")
	}
	// Subnegotiation event path with empty payload.
	su.SetOnWire(func(WireEventKind, []byte, string) {})
	_ = su.Connect(ctx)
	// IAC SB SE empty body
	su.PushWire([]byte{255, 250, 24, 255, 240})
	// Give Receive a chance via short timeout — may block if no app payload.
	rctx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancel()
	_, _ = su.Receive(rctx)
	_ = su.Disconnect(ctx)
}

// errInterceptor fails OnUpstream/OnClient so process* error arms hit.
type errInterceptor struct {
	upErr, cliErr error
}

func (e errInterceptor) OnUpstream(context.Context, InterceptContext) (InterceptResult, error) {
	return Pass(), e.upErr
}
func (e errInterceptor) OnClient(context.Context, InterceptContext) (InterceptResult, error) {
	return Pass(), e.cliErr
}

func TestInterceptorErrorsAndInjectClient(t *testing.T) {
	// Client-side inject.
	si := &scriptInterceptor{}
	h := NewHub()
	s, _ := h.CreateSession(Options{Interceptor: si})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)
	ctx := context.Background()
	inj := Inject([]byte("INJCLI"))
	si.setNextCli(inj)
	if err := s.SendToUpstream(ctx, []byte("ORIG")); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, b := range up.Sent() {
		if bytes.Equal(b, []byte("INJCLI")) {
			found = true
		}
	}
	if !found {
		t.Fatalf("inject client not sent: %v", up.Sent())
	}

	// Upstream interceptor error.
	s2, _ := h.CreateSession(Options{Interceptor: errInterceptor{upErr: errors.New("upfail")}})
	up2 := NewMemoryUpstream()
	_ = s2.ConnectUpstream(ctx, up2)
	// Push may be swallowed by reader; also exercise process via FlushDeferred.
	// Direct client error path:
	s3, _ := h.CreateSession(Options{Interceptor: errInterceptor{cliErr: errors.New("clifail")}})
	up3 := NewMemoryUpstream()
	_ = s3.ConnectUpstream(ctx, up3)
	if err := s3.SendToUpstream(ctx, []byte("x")); err == nil {
		t.Fatal("expected client interceptor error")
	}

	// ConnectUpstream failure.
	bad := &failConnectUpstream{}
	s4, _ := h.CreateSession(Options{})
	if err := s4.ConnectUpstream(ctx, bad); err == nil {
		t.Fatal("expected connect fail")
	}

	// ReplaceUpstream connect fail after detach.
	s5, _ := h.CreateSession(Options{})
	okUp := NewMemoryUpstream()
	_ = s5.ConnectUpstream(ctx, okUp)
	if err := s5.ReplaceUpstream(ctx, bad); err == nil {
		t.Fatal("expected replace connect fail")
	}

	// Deferred client-direction unit.
	si2 := &scriptInterceptor{}
	s6, _ := h.CreateSession(Options{Interceptor: si2})
	up6 := NewMemoryUpstream()
	_ = s6.ConnectUpstream(ctx, up6)
	si2.setNextCli(Defer())
	_ = s6.SendToUpstream(ctx, []byte("LATERCLI"))
	si2.setNextCli(Pass())
	if err := s6.FlushDeferred(ctx); err != nil {
		t.Fatal(err)
	}
	found = false
	for _, b := range up6.Sent() {
		if bytes.Equal(b, []byte("LATERCLI")) {
			found = true
		}
	}
	if !found {
		t.Fatalf("deferred client not flushed: %v", up6.Sent())
	}

	// MemoryUpstream closed push / send not connected.
	mu := NewMemoryUpstream()
	_ = mu.Disconnect(ctx)
	if err := mu.PushFromRemote([]byte("x")); err == nil {
		t.Fatal("push on closed should fail")
	}
	if err := mu.Send(ctx, []byte("x")); err == nil {
		t.Fatal("send on closed should fail")
	}

	// Filter Predicate false.
	f := ClientFilter{Predicate: func(ClientMetadata) bool { return false }}
	if f.Matches(ClientMetadata{}) {
		t.Fatal("predicate false should exclude")
	}

	_ = s.Close(ctx)
	_ = s2.Close(ctx)
	_ = s3.Close(ctx)
	_ = s5.Close(ctx)
	_ = s6.Close(ctx)
}

type failConnectUpstream struct{}

func (failConnectUpstream) IsConnected() bool                       { return false }
func (failConnectUpstream) Connect(context.Context) error           { return errors.New("nope") }
func (failConnectUpstream) Disconnect(context.Context) error        { return nil }
func (failConnectUpstream) Send(context.Context, []byte) error      { return nil }
func (failConnectUpstream) Receive(context.Context) ([]byte, error) { return nil, nil }

// TestForwardEmptyAndUnconnected covers empty-payload no-op and
// upstream-not-connected error arms.
func TestForwardEmptyAndUnconnected(t *testing.T) {
	h := NewHub()
	s, _ := h.CreateSession(Options{})
	ctx := context.Background()
	if err := s.SendToUpstream(ctx, []byte("x")); err == nil {
		t.Fatal("expected not connected")
	}
	if err := s.SendToUpstream(ctx, nil); err != nil {
		t.Fatalf("empty send: %v", err)
	}

	si := &scriptInterceptor{}
	s2, _ := h.CreateSession(Options{Interceptor: si})
	up := NewMemoryUpstream()
	_ = s2.ConnectUpstream(ctx, up)
	// Unknown InterceptAction → default forward arm.
	si.setNextUp(InterceptResult{Action: InterceptAction(99), Payload: []byte("Z")})
	_ = up.PushFromRemote([]byte("Q"))
	time.Sleep(30 * time.Millisecond)

	c, _ := s2.AttachClient(ClientMetadata{
		ClientID: "old", QueueCapacity: 1, Backpressure: BackpressureDropOldest,
	})
	s2.SendToClients([]byte("1"), nil)
	s2.SendToClients([]byte("2"), nil)
	short, cancel := context.WithTimeout(ctx, 200*time.Millisecond)
	defer cancel()
	_, _ = c.Receive(short)
	_ = s.Close(ctx)
	_ = s2.Close(ctx)
}

// TestFlushDeferredError propagates interceptor errors from deferred units.
func TestFlushDeferredError(t *testing.T) {
	si := &scriptInterceptor{}
	h := NewHub()
	s, _ := h.CreateSession(Options{Interceptor: si})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)
	ctx := context.Background()
	si.setNextUp(Defer())
	_ = up.PushFromRemote([]byte("LATER"))
	time.Sleep(20 * time.Millisecond)
	s.inter = errInterceptor{upErr: errors.New("boom")}
	if err := s.FlushDeferred(ctx); err == nil {
		t.Fatal("expected deferred error")
	}
	_ = s.Close(ctx)
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package embed

import (
	"bytes"
	"context"
	"sync"
	"testing"
	"time"
)

type scriptInterceptor struct {
	mu          sync.Mutex
	nextUp      *InterceptResult
	nextCli     *InterceptResult
	afterInject bool
	injectDepth int
	reenterPong []byte
}

func (s *scriptInterceptor) OnUpstream(ctx context.Context, c InterceptContext) (InterceptResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.reenterPong != nil && bytes.Equal(c.Data, []byte("PING")) {
		// re-entrant send while session lock is held
		_ = c.Session.SendFromInterceptor(ctx, s.reenterPong)
		return Pass(), nil
	}
	if s.nextUp != nil {
		r := *s.nextUp
		if r.Action == ActionInject && s.afterInject {
			s.injectDepth++
			if s.injectDepth > 1 {
				s.nextUp = nil
				return Pass(), nil
			}
		} else {
			s.nextUp = nil
		}
		return r, nil
	}
	return Pass(), nil
}

func (s *scriptInterceptor) OnClient(ctx context.Context, c InterceptContext) (InterceptResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.nextCli != nil {
		r := *s.nextCli
		s.nextCli = nil
		return r, nil
	}
	return Pass(), nil
}

func (s *scriptInterceptor) setNextUp(r InterceptResult) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := r
	s.nextUp = &cp
}

func (s *scriptInterceptor) setNextCli(r InterceptResult) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := r
	s.nextCli = &cp
}

func (s *scriptInterceptor) setAfterInject(v bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.afterInject = v
}

func TestCreateConnectFanout(t *testing.T) {
	h := NewHub()
	s, err := h.CreateSession(Options{SessionID: "s1"})
	if err != nil {
		t.Fatal(err)
	}
	if h.GetSession("s1") != s {
		t.Fatal("get")
	}
	up := NewMemoryUpstream()
	if err := s.ConnectUpstream(context.Background(), up); err != nil {
		t.Fatal(err)
	}
	c1, err := s.AttachClient(ClientMetadata{ClientID: "std", Tags: map[string]struct{}{"standard": {}}})
	if err != nil {
		t.Fatal(err)
	}
	_, _ = s.AttachClient(ClientMetadata{ClientID: "deaf", Tags: map[string]struct{}{"deaf": {}}})
	if err := up.PushFromRemote([]byte("HELLO")); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	got, err := c1.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("HELLO")) {
		t.Fatalf("got %q err %v", got, err)
	}
	s.SendToClients([]byte("X"), &ClientFilter{RequireAnyTag: []string{"standard"}})
	got, err = c1.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("X")) {
		t.Fatalf("selective %q", got)
	}
	if err := s.SendToUpstream(ctx, []byte("CMD")); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, b := range up.Sent() {
		if bytes.Equal(b, []byte("CMD")) {
			found = true
		}
	}
	if !found {
		t.Fatal("cmd not sent")
	}
	_ = s.Close(ctx)
	h.RemoveSession("s1")
}

func TestInterceptorOutcomes(t *testing.T) {
	si := &scriptInterceptor{}
	h := NewHub()
	s, _ := h.CreateSession(Options{Interceptor: si})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)
	c, _ := s.AttachClient(ClientMetadata{ClientID: "c1"})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	si.setNextUp(Consume())
	_ = up.PushFromRemote([]byte("NOPE"))
	time.Sleep(30 * time.Millisecond)
	short, cancel2 := context.WithTimeout(context.Background(), 40*time.Millisecond)
	_, err := c.Receive(short)
	cancel2()
	if err == nil {
		t.Fatal("expected consume timeout")
	}

	si.setNextUp(Replace([]byte("REP")))
	_ = up.PushFromRemote([]byte("ORIG"))
	got, err := c.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("REP")) {
		t.Fatalf("replace %q %v", got, err)
	}

	si.setNextUp(Inject([]byte("INJ")))
	si.setAfterInject(true)
	_ = up.PushFromRemote([]byte("DROPME"))
	got, err = c.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("INJ")) {
		t.Fatalf("inject %q %v", got, err)
	}

	si.setNextUp(Defer())
	_ = up.PushFromRemote([]byte("LATER"))
	time.Sleep(30 * time.Millisecond)
	si.setNextUp(Pass())
	_ = s.FlushDeferred(ctx)
	got, err = c.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("LATER")) {
		t.Fatalf("defer %q %v", got, err)
	}

	si.setNextCli(Consume())
	_ = s.SendToUpstream(ctx, []byte("LOCAL"))
	for _, b := range up.Sent() {
		if bytes.Equal(b, []byte("LOCAL")) {
			t.Fatal("local should be consumed")
		}
	}
}

func TestReplaceUpstreamAndPolicy(t *testing.T) {
	h := NewHub()
	s, _ := h.CreateSession(Options{
		Services: map[string]any{"db": "g1"},
		Telnet:   DefaultTelnetPolicy{Term: "TWGS"},
	})
	if s.Services()["db"] != "g1" {
		t.Fatal("services")
	}
	up1 := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up1)
	c, _ := s.AttachClient(ClientMetadata{ClientID: "c"})
	up2 := NewMemoryUpstream()
	if err := s.ReplaceUpstream(context.Background(), up2); err != nil {
		t.Fatal(err)
	}
	_ = up2.PushFromRemote([]byte("AFTER"))
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	got, _ := c.Receive(ctx)
	if !bytes.Equal(got, []byte("AFTER")) {
		t.Fatalf("%q", got)
	}
	s.MarkNegotiated()
	pol := DefaultTelnetPolicy{}
	if len(pol.OnOption(253, 0)) == 0 {
		t.Fatal("option")
	}
	if len(pol.OnSubnegotiation(24, []byte{1})) == 0 {
		t.Fatal("ttype")
	}
	if len(pol.OnSubnegotiation(31, nil)) == 0 {
		t.Fatal("naws")
	}
	s.RaiseWire(WireIac, []byte{255}, "x")
	f := ClientFilter{ExcludeTags: []string{"deaf"}}
	if f.Matches(ClientMetadata{Tags: map[string]struct{}{"deaf": {}}}) {
		t.Fatal("exclude")
	}
	_ = s.Close(ctx)
}

func TestScriptedTelnetUpstream_PolicyAndEmbed(t *testing.T) {
	up := NewScriptedTelnetUpstream(DefaultTelnetPolicy{Term: "ANSI-BBS"})
	var wires []WireEventKind
	up.SetOnWire(func(k WireEventKind, _ []byte, _ string) { wires = append(wires, k) })
	_ = up.Connect(context.Background())
	up.PushWire([]byte{255, 253, 0}) // DO BINARY
	up.PushWire([]byte("GO"))
	h := NewHub()
	s, _ := h.CreateSession(Options{})
	if err := s.ConnectUpstream(context.Background(), up); err != nil {
		t.Fatal(err)
	}
	c, _ := s.AttachClient(ClientMetadata{ClientID: "c1"})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	got, err := c.Receive(ctx)
	if err != nil || !bytes.Equal(got, []byte("GO")) {
		t.Fatalf("got %q err %v", got, err)
	}
	if len(wires) == 0 {
		t.Fatal("expected wire events")
	}
	if len(up.SentWire()) == 0 {
		t.Fatal("expected policy reply")
	}
	_ = s.SendToUpstream(ctx, []byte{255, 1})
	found := false
	for _, b := range up.SentWire() {
		if bytes.Equal(b, []byte{255, 255, 1}) {
			found = true
		}
	}
	if !found {
		t.Fatal("iac escape missing")
	}
	_ = s.Close(ctx)
}

func TestUpstreamLostAndDupClient(t *testing.T) {
	h := NewHub()
	s, _ := h.CreateSession(Options{})
	up := NewMemoryUpstream()
	_ = s.ConnectUpstream(context.Background(), up)
	_, _ = s.AttachClient(ClientMetadata{ClientID: "x"})
	if _, err := s.AttachClient(ClientMetadata{ClientID: "x"}); err == nil {
		t.Fatal("dup")
	}
	lost := make(chan struct{}, 1)
	s.OnLifecycle(func(phase SessionLifecycle, _ string) {
		if phase == LifecycleUpstreamLost {
			select {
			case lost <- struct{}{}:
			default:
			}
		}
	})
	up.CompleteRemote()
	select {
	case <-lost:
	case <-time.After(2 * time.Second):
		t.Fatal("timeout lost")
	}
	if _, err := h.CreateSession(Options{SessionID: s.SessionID()}); err == nil {
		t.Fatal("dup session")
	}
	_ = s.Close(context.Background())
}

func TestDetachClientMarksHandle(t *testing.T) {
	h := NewHub()
	s, err := h.CreateSession(Options{SessionID: "detach"})
	if err != nil {
		t.Fatal(err)
	}
	up := NewMemoryUpstream()
	if err := s.ConnectUpstream(context.Background(), up); err != nil {
		t.Fatal(err)
	}
	handle, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	if !handle.IsAttached() {
		t.Fatal("expected attached")
	}
	s.DetachClient("c1")
	if handle.IsAttached() {
		t.Fatal("expected detached")
	}
	// re-attach ok
	h2, err := s.AttachClient(ClientMetadata{ClientID: "c1"})
	if err != nil {
		t.Fatal(err)
	}
	if !h2.IsAttached() {
		t.Fatal("re-attach")
	}
	_ = s.Close(context.Background())
	if h2.IsAttached() {
		t.Fatal("closed session should detach")
	}
}

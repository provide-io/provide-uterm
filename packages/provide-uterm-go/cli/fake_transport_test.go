//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// fakeTransport is an in-memory ConnectionTransport for tests. Received data is
// fed via the inbound channel; sent data is recorded.
type fakeTransport struct {
	mu        sync.Mutex
	connected bool
	sent      [][]byte
	inbound   chan []byte
}

func newFakeTransport() *fakeTransport {
	return &fakeTransport{inbound: make(chan []byte, 16)}
}

func (f *fakeTransport) Connect(_ context.Context, _ string, _ int, _ transports.ConnectOptions) error {
	f.mu.Lock()
	f.connected = true
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) Disconnect(_ context.Context) error {
	f.mu.Lock()
	f.connected = false
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) Send(_ context.Context, data []byte) error {
	f.mu.Lock()
	f.sent = append(f.sent, append([]byte(nil), data...))
	f.mu.Unlock()
	return nil
}

func (f *fakeTransport) Receive(ctx context.Context, _ int, timeout time.Duration) ([]byte, error) {
	select {
	case <-ctx.Done():
		return nil, transports.ErrConnectionClosed
	case d := <-f.inbound:
		return d, nil
	case <-time.After(timeout):
		return nil, nil
	}
}

func (f *fakeTransport) IsConnected() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.connected
}

// newFakeSession wraps a fakeTransport in a connected TransportSession.
func newFakeSession() *termsession.TransportSession {
	ft := newFakeTransport()
	s := termsession.New(ft, func(ctx context.Context) error {
		return ft.Connect(ctx, "", 0, transports.ConnectOptions{})
	}, termsession.Options{})
	_ = s.Connect(context.Background())
	return s
}

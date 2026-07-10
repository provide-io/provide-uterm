//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// errBoom is a sentinel dial error for the failed-Start test.
var errBoom = errors.New("boom")

// fakeTransport is an in-memory ConnectionTransport for connector unit tests.
// Received data is fed via inbound; sent data is recorded.
type fakeTransport struct {
	mu         sync.Mutex
	connected  bool
	connectErr error
	sent       [][]byte
	inbound    chan []byte
}

func newFakeTransport() *fakeTransport {
	return &fakeTransport{inbound: make(chan []byte, 16)}
}

func (f *fakeTransport) Connect(_ context.Context, _ string, _ int, _ transports.ConnectOptions) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.connectErr != nil {
		return f.connectErr
	}
	f.connected = true
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

func (f *fakeTransport) sentCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

// newFakeConnector builds a transportConnector over a fake transport, returning
// the connector and the underlying transport so tests can inject inbound data.
func newFakeConnector(inputMode string) (*transportConnector, *fakeTransport) {
	ft := newFakeTransport()
	build := func() *termsession.TransportSession {
		return termsession.New(ft, func(ctx context.Context) error {
			return ft.Connect(ctx, "", 0, transports.ConnectOptions{})
		}, termsession.Options{})
	}
	c := newTransportConnector("fake-sess", "Fake", "fake", "fake://local", inputMode, build)
	return c, ft
}

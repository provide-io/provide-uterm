//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
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

// fakeConnector is an already-started connectors.Connector over a fake in-memory
// session, so the registry lifecycle tests exercise real connector wiring
// without needing a live shell/remote. It satisfies connectors.Connector.
type fakeConnector struct {
	mu   sync.Mutex
	sess *termsession.TransportSession
	mode string
}

// newFakeConnector returns a connected fake connector.
func newFakeConnector() *fakeConnector {
	return &fakeConnector{sess: newFakeSession(), mode: "open"}
}

func (c *fakeConnector) Start(context.Context) error { return nil }

func (c *fakeConnector) Stop(ctx context.Context) error {
	c.mu.Lock()
	sess := c.sess
	c.sess = nil
	c.mu.Unlock()
	if sess != nil {
		return sess.Close(ctx)
	}
	return nil
}

func (c *fakeConnector) IsConnected() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sess != nil && c.sess.IsConnected()
}

func (c *fakeConnector) HandleInput(ctx context.Context, data string) error {
	c.mu.Lock()
	sess := c.sess
	c.mu.Unlock()
	if sess == nil {
		return nil
	}
	return sess.Send(ctx, data)
}

func (c *fakeConnector) HandleControl(string) error { return nil }

func (c *fakeConnector) SetMode(mode string) error {
	c.mu.Lock()
	c.mode = mode
	c.mu.Unlock()
	return nil
}

func (c *fakeConnector) Clear() error { return nil }

func (c *fakeConnector) Snapshot() session.Snapshot {
	c.mu.Lock()
	sess := c.sess
	c.mu.Unlock()
	if sess == nil {
		return session.Snapshot{}
	}
	return sess.Snapshot()
}

func (c *fakeConnector) Analysis() string { return "fake connector analysis" }

func (c *fakeConnector) Events() []map[string]any { return nil }

func (c *fakeConnector) Session() *termsession.TransportSession {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.sess
}

var _ connectors.Connector = (*fakeConnector)(nil)

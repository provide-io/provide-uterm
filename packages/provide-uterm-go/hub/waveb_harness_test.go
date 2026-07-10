//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// fakeBrowserWS is a comparable (pointer-identity) browser socket implementing
// BrowserSender + BrowserCloser. It records every payload it receives and can
// be told to fail sends (to exercise the dead-socket pruning path).
type fakeBrowserWS struct {
	name string

	mu        sync.Mutex
	sent      []string
	failSend  error
	closed    bool
	closeCode int
	closeMsg  string
	principal any // *Principal for quota tests (via UtermPrincipal)
}

func newBrowserWS(name string) *fakeBrowserWS { return &fakeBrowserWS{name: name} }

func (b *fakeBrowserWS) SendText(_ context.Context, payload string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.failSend != nil {
		return b.failSend
	}
	b.sent = append(b.sent, payload)
	return nil
}

func (b *fakeBrowserWS) Close(_ context.Context, code int, reason string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.closed = true
	b.closeCode = code
	b.closeMsg = reason
	return nil
}

// UtermPrincipal implements principalCarrier for per-principal quota tests.
func (b *fakeBrowserWS) UtermPrincipal() any { return b.principal }

func (b *fakeBrowserWS) payloads() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]string(nil), b.sent...)
}

func (b *fakeBrowserWS) last() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.sent) == 0 {
		return ""
	}
	return b.sent[len(b.sent)-1]
}

func (b *fakeBrowserWS) count() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.sent)
}

// fakeWorkerWS is a worker socket implementing WorkerWS + WorkerCloser.
type fakeWorkerWS struct {
	mu       sync.Mutex
	sent     []string
	failSend error
	closed   bool
	closeErr error
}

func (w *fakeWorkerWS) SendText(_ context.Context, payload string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.failSend != nil {
		return w.failSend
	}
	w.sent = append(w.sent, payload)
	return nil
}

func (w *fakeWorkerWS) Close(_ context.Context) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.closed = true
	return w.closeErr
}

func (w *fakeWorkerWS) payloads() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.sent...)
}

func (w *fakeWorkerWS) last() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.sent) == 0 {
		return ""
	}
	return w.sent[len(w.sent)-1]
}

// fakeTunnelWS is a worker socket that speaks the binary tunnel protocol.
type fakeTunnelWS struct {
	mu          sync.Mutex
	inputs      []string
	httpControl []map[string]any
}

func (t *fakeTunnelWS) SendText(context.Context, string) error { return nil }

func (t *fakeTunnelWS) SendInput(_ context.Context, data string) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.inputs = append(t.inputs, data)
	return nil
}

func (t *fakeTunnelWS) SendHTTPControl(_ context.Context, msg map[string]any) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.httpControl = append(t.httpControl, msg)
	return nil
}

// newTestHub builds a TermHub with a manual clock (mono=1000, wall=5000), a
// discard logger, and any config overrides applied via mutate.
func newTestHub(t *testing.T, mutate func(*TermHubConfig)) (*TermHub, *ManualClock) {
	t.Helper()
	clk := NewManualClock(5000)
	clk.SetMonotonic(1000)
	cfg := TermHubConfig{Clock: clk, Logger: discardLogger()}
	if mutate != nil {
		mutate(&cfg)
	}
	return NewTermHub(cfg), clk
}

// ctx returns a background context for tests.
func bg() context.Context { return context.Background() }

// decodeOneControl decodes the single inline control frame in payload.
func decodeOneControl(t *testing.T, payload string) map[string]any {
	t.Helper()
	return decodeControlPayload(t, payload)
}

// registerWorkerState puts a worker with a live worker socket into the registry.
func registerWorkerState(h *TermHub, workerID string, ws WorkerWS) *WorkerTermState {
	st := NewWorkerTermState()
	st.WorkerWS = ws
	h.registry.Put(workerID, st)
	return st
}

// decodeTerminalData decodes the single data chunk in payload.
func decodeTerminalData(t *testing.T, payload string) string {
	t.Helper()
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	events, err := dec.Feed(payload)
	if err != nil {
		t.Fatalf("feed: %v", err)
	}
	fin, err := dec.Finish()
	if err != nil {
		t.Fatalf("finish: %v", err)
	}
	events = append(events, fin...)
	out := ""
	for _, e := range events {
		if d, ok := e.(controlchannel.DataChunk); ok {
			out += d.Data
		}
	}
	return out
}

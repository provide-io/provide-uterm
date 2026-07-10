//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"sync"
)

type mockSession struct {
	mu       sync.Mutex
	watches  []WatchFunc
	sent     []string
	sizes    [][2]int
	snapshot map[string]any
	sendErr  error
	sizeErr  error
}

func (s *mockSession) AddWatch(fn WatchFunc) {
	s.mu.Lock()
	s.watches = append(s.watches, fn)
	s.mu.Unlock()
}

func (s *mockSession) Send(_ context.Context, data string) error {
	if s.sendErr != nil {
		return s.sendErr
	}
	s.mu.Lock()
	s.sent = append(s.sent, data)
	s.mu.Unlock()
	return nil
}

func (s *mockSession) SetSize(_ context.Context, cols, rows int) error {
	if s.sizeErr != nil {
		return s.sizeErr
	}
	s.mu.Lock()
	s.sizes = append(s.sizes, [2]int{cols, rows})
	s.mu.Unlock()
	return nil
}

func (s *mockSession) Snapshot() map[string]any { return s.snapshot }

func (s *mockSession) sentKeys() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.sent...)
}

func (s *mockSession) allSizes() [][2]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([][2]int(nil), s.sizes...)
}

func (s *mockSession) watchCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.watches)
}

func (s *mockSession) fire(snapshot map[string]any, raw []byte) {
	s.mu.Lock()
	watches := append([]WatchFunc(nil), s.watches...)
	s.mu.Unlock()
	for _, w := range watches {
		w(snapshot, raw)
	}
}

type mockWorker struct {
	mu          sync.Mutex
	session     Session
	hijackCalls []bool
	stepCalls   int
	hijackErr   error
	stepErr     error
}

func (w *mockWorker) Session() Session { return w.session }

func (w *mockWorker) SetHijacked(_ context.Context, enabled bool) error {
	w.mu.Lock()
	w.hijackCalls = append(w.hijackCalls, enabled)
	w.mu.Unlock()
	return w.hijackErr
}

func (w *mockWorker) RequestStep(_ context.Context) error {
	w.mu.Lock()
	w.stepCalls++
	w.mu.Unlock()
	return w.stepErr
}

func (w *mockWorker) calls() []bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]bool(nil), w.hijackCalls...)
}

func (w *mockWorker) steps() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.stepCalls
}

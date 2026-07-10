//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// defaultRoleResolveTimeout is the deadline applied to an awaitable browser
// role resolver. Port of the hardcoded 5.0s in resolve_role_for_browser.
const defaultRoleResolveTimeout = 5 * time.Second

// BackgroundTasks tracks cancellable background goroutines for graceful
// shutdown. It is the Go analogue of the Python hub's “_background_tasks“ set
// consumed by StateStore.Shutdown. Wave B registers its loops here.
type BackgroundTasks struct {
	mu      sync.Mutex
	entries []bgEntry
}

type bgEntry struct {
	cancel context.CancelFunc
	result <-chan error
}

// Add registers a running task by its cancel func and a channel that yields the
// task's terminal error (nil = completed normally; non-nil = cancelled/errored).
func (bt *BackgroundTasks) Add(cancel context.CancelFunc, result <-chan error) {
	bt.mu.Lock()
	defer bt.mu.Unlock()
	bt.entries = append(bt.entries, bgEntry{cancel: cancel, result: result})
}

// shutdown cancels and awaits every registered task, returning the count that
// ended with an error (cancelled or raised) — matching the Python
// shutdown_background_tasks count semantics.
func (bt *BackgroundTasks) shutdown() int {
	bt.mu.Lock()
	entries := bt.entries
	bt.entries = nil
	bt.mu.Unlock()
	if len(entries) == 0 {
		return 0
	}
	for _, e := range entries {
		e.cancel()
	}
	count := 0
	for _, e := range entries {
		if err := <-e.result; err != nil {
			count++
		}
	}
	return count
}

// StateStoreConfig injects the hub surface StateStore depends on. Lock is the
// composing hub's shared mutex; Registry the worker map; the callbacks and
// identity plumbing are the same the Python store reaches on its hub back
// reference.
type StateStoreConfig struct {
	Registry           *WorkerRegistry
	Lock               *sync.Mutex
	Clock              Clock
	Logger             *slog.Logger
	MaxBufferChars     int
	OnMetric           func(name string, value int)
	OnHijackChanged    func(workerID string, enabled bool, owner *string) error
	ResolveBrowserRole RoleResolver
	IdentityProvider   IdentityProvider
	DelegateRoles      bool
	// RoleResolveTimeout overrides the 5s awaitable-resolver deadline (0 uses
	// the default).
	RoleResolveTimeout time.Duration
	// Tasks is the background-task registry drained by Shutdown (nil allocates one).
	Tasks *BackgroundTasks
}

// StateStore is the input-buffer + lifecycle/policy helper service. Port of
// provide.uterm.server.bridge.hub.store.StateStore.
type StateStore struct {
	registry           *WorkerRegistry
	lock               *sync.Mutex
	clock              Clock
	logger             *slog.Logger
	maxBufferChars     int
	onMetric           func(string, int)
	onHijackChanged    func(string, bool, *string) error
	resolveBrowserRole RoleResolver
	identityProvider   IdentityProvider
	delegateRoles      bool
	roleResolveTimeout time.Duration
	tasks              *BackgroundTasks

	inputBuffersMu sync.Mutex
	inputBuffers   map[BrowserConn]string
}

// NewStateStore builds a StateStore from cfg.
func NewStateStore(cfg StateStoreConfig) *StateStore {
	timeout := cfg.RoleResolveTimeout
	if timeout <= 0 {
		timeout = defaultRoleResolveTimeout
	}
	tasks := cfg.Tasks
	if tasks == nil {
		tasks = &BackgroundTasks{}
	}
	return &StateStore{
		registry:           cfg.Registry,
		lock:               cfg.Lock,
		clock:              orDefaultClock(cfg.Clock),
		logger:             loggerOrDefault(cfg.Logger),
		maxBufferChars:     cfg.MaxBufferChars,
		onMetric:           cfg.OnMetric,
		onHijackChanged:    cfg.OnHijackChanged,
		resolveBrowserRole: cfg.ResolveBrowserRole,
		identityProvider:   cfg.IdentityProvider,
		delegateRoles:      cfg.DelegateRoles,
		roleResolveTimeout: timeout,
		tasks:              tasks,
		inputBuffers:       map[BrowserConn]string{},
	}
}

// Tasks returns the background-task registry (so wave B can register loops).
func (s *StateStore) Tasks() *BackgroundTasks { return s.tasks }

// BufferAndGetCommand accumulates input for ws and returns the command once a
// CR/LF is seen. A buffer that would exceed MaxBufferChars is discarded (empty
// return). Port of buffer_and_get_command. Returns (command, ok).
func (s *StateStore) BufferAndGetCommand(ws BrowserConn, data string) (string, bool) {
	s.inputBuffersMu.Lock()
	defer s.inputBuffersMu.Unlock()
	buf := s.inputBuffers[ws] + data
	if len(buf) > s.maxBufferChars {
		delete(s.inputBuffers, ws)
		return "", false
	}
	if containsAny(buf, "\r\n") {
		delete(s.inputBuffers, ws)
		return buf, true
	}
	s.inputBuffers[ws] = buf
	return "", false
}

// containsAny reports whether s contains any byte in chars.
func containsAny(s, chars string) bool {
	for i := 0; i < len(s); i++ {
		for j := 0; j < len(chars); j++ {
			if s[i] == chars[j] {
				return true
			}
		}
	}
	return false
}

// Shutdown cancels all background tasks and returns the number cancelled. Port
// of StateStore.shutdown (which returns None; this port returns the count for
// testability).
func (s *StateStore) Shutdown() int {
	count := s.tasks.shutdown()
	if count != 0 {
		s.logger.Info("hub_shutdown", "cancelled_background_tasks", count)
	}
	return count
}

// TouchActivity updates the last-activity timestamp for workerID.
func (s *StateStore) TouchActivity(workerID string) {
	s.lock.Lock()
	defer s.lock.Unlock()
	st := s.registry.Get(workerID)
	if st != nil {
		st.LastActivityAt = s.clock.Monotonic()
	}
}

// GetOrCreate returns the existing worker state for workerID or creates one.
func (s *StateStore) GetOrCreate(workerID string) *WorkerTermState {
	s.lock.Lock()
	defer s.lock.Unlock()
	st := s.registry.Get(workerID)
	if st == nil {
		st = NewWorkerTermState()
		s.registry.Put(workerID, st)
	}
	return st
}

// Metric emits a named metric via the configured callback, swallowing (and
// logging) a panicking callback. Port of StateStore.metric.
//
// Deviation: the Python default value is 1 and the callback receives int(value)
// (truncating a float). Go is statically typed, so callers pass an explicit int
// value; the float-truncation branch has no Go analogue.
func (s *StateStore) Metric(name string, value int) {
	if s.onMetric == nil {
		return
	}
	defer func() {
		if r := recover(); r != nil {
			s.logger.Warn("metric_callback_failed", "metric", name, "error", r)
		}
	}()
	s.onMetric(name, value)
}

// ClampLease clamps a lease duration into [1, 14400] seconds. Port of clamp_lease.
func ClampLease(leaseS int) int {
	if leaseS < 1 {
		return 1
	}
	if leaseS > 14400 {
		return 14400
	}
	return leaseS
}

// HasValidRESTLease reports whether st has an unexpired REST hijack session.
func (s *StateStore) HasValidRESTLease(st *WorkerTermState) bool {
	hs := st.HijackSession
	return hs != nil && hs.LeaseExpiresAt > s.clock.Monotonic()
}

// IsDashboardHijackActive reports whether a dashboard-WS owner exists with an
// unexpired (or perpetual) lease.
func (s *StateStore) IsDashboardHijackActive(st *WorkerTermState) bool {
	if st.HijackOwner == nil {
		return false
	}
	if st.HijackOwnerExpiresAt == nil {
		return true
	}
	return *st.HijackOwnerExpiresAt > s.clock.Monotonic()
}

// IsHijacked reports whether st is under any active hijack (dashboard or REST).
func (s *StateStore) IsHijacked(st *WorkerTermState) bool {
	return s.IsDashboardHijackActive(st) || s.HasValidRESTLease(st)
}

// NotifyHijackChanged fires the on-hijack-changed callback, logging a warning
// if it returns an error. Port of notify_hijack_changed.
//
// Deviation: the Python callback may be sync or an awaitable fired-and-forgotten;
// this port models it as a synchronous callback returning an error (a non-nil
// error is logged, mirroring the async done-callback that logs a raised error).
func (s *StateStore) NotifyHijackChanged(workerID string, enabled bool, owner *string) {
	if s.onHijackChanged == nil {
		return
	}
	if err := s.onHijackChanged(workerID, enabled, owner); err != nil {
		s.logger.Warn("on_hijack_changed callback raised", "worker_id", workerID, "error", err)
	}
}

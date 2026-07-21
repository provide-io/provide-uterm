//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"fmt"
	"sort"
	"sync"
)

// ErrWorkerNotFound is returned by [WorkerRegistry.Require] for an unknown
// worker. It wraps the worker id (the Go analogue of the Python
// “KeyError(worker_id)“); callers can read the id via [ErrWorkerNotFound.WorkerID].
type ErrWorkerNotFound struct {
	WorkerID string
}

func (e *ErrWorkerNotFound) Error() string {
	return fmt.Sprintf("worker not found: %q", e.WorkerID)
}

// WorkerRegistry is an in-memory registry of attached workers keyed by
// worker id. Port of provide.uterm.server.bridge.hub.registry.WorkerRegistry.
//
// The Python registry takes no locks and relies on the hub lock for
// coordination. This port guards its own map with an RWMutex so the registry
// is independently safe under `go test -race`; mutation of the returned
// [WorkerTermState] fields is still expected to be serialised by the composing
// hub's shared mutex, exactly as in Python.
type WorkerRegistry struct {
	mu      sync.RWMutex
	workers map[string]*WorkerTermState
}

// NewWorkerRegistry returns an empty registry.
func NewWorkerRegistry() *WorkerRegistry {
	return &WorkerRegistry{workers: map[string]*WorkerTermState{}}
}

// Get returns the state for workerID, or nil if unknown.
func (r *WorkerRegistry) Get(workerID string) *WorkerTermState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.workers[workerID]
}

// Require returns the state for workerID, or an [ErrWorkerNotFound] if absent.
func (r *WorkerRegistry) Require(workerID string) (*WorkerTermState, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	st, ok := r.workers[workerID]
	if !ok {
		return nil, &ErrWorkerNotFound{WorkerID: workerID}
	}
	return st, nil
}

// Put inserts or replaces the state for workerID.
func (r *WorkerRegistry) Put(workerID string, state *WorkerTermState) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.workers[workerID] = state
}

// SetDefault returns the existing state for workerID, or inserts state and
// returns it. Mirrors dict.setdefault.
func (r *WorkerRegistry) SetDefault(workerID string, state *WorkerTermState) *WorkerTermState {
	r.mu.Lock()
	defer r.mu.Unlock()
	if existing, ok := r.workers[workerID]; ok {
		return existing
	}
	r.workers[workerID] = state
	return state
}

// Pop removes and returns the state for workerID, or nil if absent.
func (r *WorkerRegistry) Pop(workerID string) *WorkerTermState {
	r.mu.Lock()
	defer r.mu.Unlock()
	st, ok := r.workers[workerID]
	if !ok {
		return nil
	}
	if st.GraphicalSession != nil {
		if closer, isCloser := st.GraphicalSession.(interface{ Close() error }); isCloser {
			closer.Close()
		}
	}
	delete(r.workers, workerID)
	return st
}

// Discard removes workerID if present and reports whether it was removed.
func (r *WorkerRegistry) Discard(workerID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	st, ok := r.workers[workerID]
	if !ok {
		return false
	}
	if st.GraphicalSession != nil {
		if closer, isCloser := st.GraphicalSession.(interface{ Close() error }); isCloser {
			closer.Close()
		}
	}
	delete(r.workers, workerID)
	return true
}

// Contains reports whether workerID is registered.
func (r *WorkerRegistry) Contains(workerID string) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	_, ok := r.workers[workerID]
	return ok
}

// All returns a snapshot list of all registered worker states (unordered).
func (r *WorkerRegistry) All() []*WorkerTermState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]*WorkerTermState, 0, len(r.workers))
	for _, st := range r.workers {
		out = append(out, st)
	}
	return out
}

// Keys returns a sorted snapshot of all registered worker ids.
//
// Deviation: Python returns keys in dict insertion order. Go map iteration is
// unordered, so this port sorts the keys for a deterministic snapshot; callers
// must not rely on insertion order (none in the ported scope do).
func (r *WorkerRegistry) Keys() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.workers))
	for k := range r.workers {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Len returns the number of registered workers.
func (r *WorkerRegistry) Len() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.workers)
}

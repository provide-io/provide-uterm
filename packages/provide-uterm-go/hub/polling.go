//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"regexp"
	"sync"
)

// PollingCoordinator provides snapshot-polling helpers. Port of
// provide.uterm.server.bridge.hub.polling_service.PollingCoordinator.
//
// Snapshot requests are dispatched through the injected requestSnapshot
// callback (wave B wires it to the presence manager; the Python indirection
// through hub.request_snapshot exists so tests can intercept it). The
// registry + lock are read to observe the worker's latest snapshot.
type PollingCoordinator struct {
	registry        *WorkerRegistry
	lock            *sync.Mutex
	clock           Clock
	requestSnapshot func(ctx context.Context, workerID string) error
}

// NewPollingCoordinator builds a coordinator. lock is the composing hub's
// shared mutex; clock nil selects the real clock.
func NewPollingCoordinator(
	registry *WorkerRegistry,
	lock *sync.Mutex,
	clock Clock,
	requestSnapshot func(ctx context.Context, workerID string) error,
) *PollingCoordinator {
	return &PollingCoordinator{
		registry:        registry,
		lock:            lock,
		clock:           orDefaultClock(clock),
		requestSnapshot: requestSnapshot,
	}
}

// SnapshotMatches reports whether snapshot satisfies the prompt-id and/or regex
// guard. Port of the static snapshot_matches helper.
func SnapshotMatches(snapshot map[string]any, expectPromptID string, expectRegex *regexp.Regexp) bool {
	return snapshotMatches(snapshot, expectPromptID, expectRegex)
}

// WaitForSnapshot requests a fresh snapshot and polls until one arrives (with a
// ts newer than the request) or timeoutMs elapses. Port of wait_for_snapshot.
func (p *PollingCoordinator) WaitForSnapshot(ctx context.Context, workerID string, timeoutMs int) (map[string]any, error) {
	reqTS := p.clock.Wall()
	end := p.clock.Monotonic() + float64(timeoutMs)/1000.0
	if err := p.requestSnapshot(ctx, workerID); err != nil {
		return nil, err
	}
	for p.clock.Monotonic() < end {
		p.lock.Lock()
		st := p.registry.Get(workerID)
		if st == nil {
			p.lock.Unlock()
			return nil, nil
		}
		snap := st.LastSnapshot
		p.lock.Unlock()
		if snap != nil && coerceFloat(snap["ts"], 0) > reqTS {
			return snap, nil
		}
		if err := p.clock.Sleep(ctx, 0.08); err != nil {
			return nil, err
		}
	}
	return nil, nil
}

// compileGuardRegex compiles expectRegex, returning (pattern, errMsg). It
// returns (nil, "") when expectRegex is empty, and (nil, message) on failure.
// Port of _compile_guard_regex.
func compileGuardRegex(expectRegex string) (*regexp.Regexp, string) {
	if expectRegex == "" {
		return nil, ""
	}
	re, err := compileExpectRegex(expectRegex)
	if err != nil {
		return nil, err.Error()
	}
	return re, ""
}

// WaitForGuard polls until the snapshot satisfies the prompt-id/regex guards or
// timeoutMs elapses. Port of wait_for_guard. Returns (matched, snapshot,
// reason); reason is "" on success, else a short error string.
func (p *PollingCoordinator) WaitForGuard(
	ctx context.Context,
	workerID string,
	expectPromptID string,
	expectRegex string,
	timeoutMs int,
	pollIntervalMs int,
) (bool, map[string]any, string, error) {
	regexObj, regexErr := compileGuardRegex(expectRegex)
	if regexErr != "" {
		return false, nil, regexErr, nil
	}

	if expectPromptID == "" && regexObj == nil {
		p.lock.Lock()
		st := p.registry.Get(workerID)
		var snap map[string]any
		if st != nil {
			snap = st.LastSnapshot
		}
		p.lock.Unlock()
		if err := p.requestSnapshot(ctx, workerID); err != nil {
			return false, nil, "", err
		}
		return true, snap, "", nil
	}

	end := p.clock.Monotonic() + float64(maxInt(50, timeoutMs))/1000.0
	interval := float64(maxInt(20, pollIntervalMs)) / 1000.0
	var lastSnapshot map[string]any
	if err := p.requestSnapshot(ctx, workerID); err != nil {
		return false, nil, "", err
	}
	lastSnapTS := 0.0
	for p.clock.Monotonic() < end {
		p.lock.Lock()
		st := p.registry.Get(workerID)
		if st != nil {
			lastSnapshot = st.LastSnapshot
		} else {
			lastSnapshot = nil
		}
		p.lock.Unlock()
		if SnapshotMatches(lastSnapshot, expectPromptID, regexObj) {
			return true, lastSnapshot, "", nil
		}
		snapTS := 0.0
		if lastSnapshot != nil {
			snapTS = coerceFloat(lastSnapshot["ts"], 0.0)
		}
		if snapTS <= lastSnapTS {
			if err := p.requestSnapshot(ctx, workerID); err != nil {
				return false, nil, "", err
			}
		}
		lastSnapTS = snapTS
		if err := p.clock.Sleep(ctx, interval); err != nil {
			return false, nil, "", err
		}
	}
	return false, lastSnapshot, "prompt_guard_not_satisfied", nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

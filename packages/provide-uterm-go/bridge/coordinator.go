//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"
)

// Port of provide.uterm.bridge.coordinator: the single-writer hijack-lease
// arbitration state machine (acquire → heartbeat → release).
//
// Deviation from Python: the coordinator is guarded by a mutex so it is safe
// under Go concurrency (the Python class relies on the single asyncio thread /
// an outer async lock). Lease timestamps are monotonic seconds (time since a
// process-start reference), matching Python's time.monotonic(). Every method
// has an ...At variant that injects now for deterministic testing, exactly
// like the Python now= keyword.

var monoStart = time.Now()

// monotonicSeconds returns seconds since process start, mirroring
// time.monotonic() as a float.
func monotonicSeconds() float64 { return time.Since(monoStart).Seconds() }

// clampLease clamps a lease duration to [1, 3600] seconds. Port of _clamp_lease.
func clampLease(leaseS int) int {
	return max(1, min(leaseS, 3600))
}

// newHijackID returns a random RFC-4122 v4 UUID string, the Go equivalent of
// Python's uuid.uuid4(). It uses crypto/rand (which never fails on supported
// platforms) rather than adding a UUID dependency.
func newHijackID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	var buf [36]byte
	hex.Encode(buf[0:8], b[0:4])
	buf[8] = '-'
	hex.Encode(buf[9:13], b[4:6])
	buf[13] = '-'
	hex.Encode(buf[14:18], b[6:8])
	buf[18] = '-'
	hex.Encode(buf[19:23], b[8:10])
	buf[23] = '-'
	hex.Encode(buf[24:36], b[10:16])
	return string(buf[:])
}

// HijackSession is a live hijack lease. Port of the HijackSession dataclass.
type HijackSession struct {
	HijackID       string
	Owner          string
	LeaseExpiresAt float64
	AcquiredAt     float64
	LastHeartbeat  float64
	// AcquiredBy is the authenticated subject_id of the principal that
	// acquired this lease (REST path). Distinct from Owner (a self-declared
	// display label); empty for unauthenticated/legacy leases.
	AcquiredBy string
}

// AcquireResult is the outcome of an acquire/heartbeat/release call. Port of
// the AcquireResult dataclass.
type AcquireResult struct {
	OK bool
	// Session is the live (or echoed) session; nil when there is none.
	Session *HijackSession
	// Error is the failure reason ("already_hijacked", "not_hijacked",
	// "hijack_id_mismatch", "owner_mismatch"), or "" on success.
	Error string
	// IsRenewal is true when the same owner renewed an existing lease.
	IsRenewal bool
}

// HijackCoordinator is single-writer hijack arbitration for one worker session.
type HijackCoordinator struct {
	mu      sync.Mutex
	session *HijackSession
}

// NewHijackCoordinator returns an empty coordinator.
func NewHijackCoordinator() *HijackCoordinator { return &HijackCoordinator{} }

// activeSession returns the live session at nowTS, lazily purging an expired
// lease. Callers must hold c.mu. Port of _active_session.
func (c *HijackCoordinator) activeSession(nowTS float64) *HijackSession {
	session := c.session
	if session == nil {
		return nil
	}
	if session.LeaseExpiresAt <= nowTS {
		c.session = nil
		return nil
	}
	return session
}

// Session returns the active HijackSession, or nil, purging expiry lazily.
// Port of the .session property.
func (c *HijackCoordinator) Session() *HijackSession {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.activeSession(monotonicSeconds())
}

// Acquire acquires a hijack lease at the current monotonic time.
func (c *HijackCoordinator) Acquire(owner string, leaseS int) AcquireResult {
	return c.AcquireAt(owner, leaseS, monotonicSeconds())
}

// AcquireAt acquires a hijack lease at now, always generating a new hijack_id.
//
// If the same owner already holds the lease it is renewed with a fresh
// hijack_id (IsRenewal=true). A different owner while a lease is active returns
// OK=false with Error="already_hijacked". Port of acquire.
func (c *HijackCoordinator) AcquireAt(owner string, leaseS int, now float64) AcquireResult {
	c.mu.Lock()
	defer c.mu.Unlock()
	active := c.activeSession(now)
	if active != nil && active.Owner != owner {
		return AcquireResult{OK: false, Session: active, Error: "already_hijacked"}
	}
	isRenewal := active != nil
	session := &HijackSession{
		HijackID:       newHijackID(),
		Owner:          owner,
		LeaseExpiresAt: now + float64(clampLease(leaseS)),
		AcquiredAt:     now,
		LastHeartbeat:  now,
	}
	c.session = session
	return AcquireResult{OK: true, Session: session, IsRenewal: isRenewal}
}

// Heartbeat refreshes a lease at the current monotonic time. owner may be ""
// to skip the owner check (mirroring the Python owner=None default).
func (c *HijackCoordinator) Heartbeat(hijackID string, leaseS int, owner string) AcquireResult {
	return c.HeartbeatAt(hijackID, leaseS, owner, monotonicSeconds())
}

// HeartbeatAt refreshes a lease at now. Port of heartbeat.
func (c *HijackCoordinator) HeartbeatAt(hijackID string, leaseS int, owner string, now float64) AcquireResult {
	c.mu.Lock()
	defer c.mu.Unlock()
	active := c.activeSession(now)
	if active == nil {
		return AcquireResult{OK: false, Session: nil, Error: "not_hijacked"}
	}
	if active.HijackID != hijackID {
		return AcquireResult{OK: false, Session: active, Error: "hijack_id_mismatch"}
	}
	if owner != "" && active.Owner != owner {
		return AcquireResult{OK: false, Session: active, Error: "owner_mismatch"}
	}
	active.LeaseExpiresAt = now + float64(clampLease(leaseS))
	active.LastHeartbeat = now
	return AcquireResult{OK: true, Session: active}
}

// Release releases the lease with a matching hijack_id. Port of release. Unlike
// the read-through Session accessor it does not purge by expiry — it releases
// the raw slot exactly like the Python method.
func (c *HijackCoordinator) Release(hijackID string) AcquireResult {
	c.mu.Lock()
	defer c.mu.Unlock()
	active := c.session
	if active == nil {
		return AcquireResult{OK: false, Session: nil, Error: "not_hijacked"}
	}
	if active.HijackID != hijackID {
		return AcquireResult{OK: false, Session: active, Error: "hijack_id_mismatch"}
	}
	c.session = nil
	return AcquireResult{OK: true, Session: nil}
}

// CanSendInput reports whether hijackID owns the active lease. Port of
// can_send_input.
func (c *HijackCoordinator) CanSendInput(hijackID string) bool {
	active := c.Session()
	if active == nil {
		return false
	}
	return hijackID == active.HijackID
}

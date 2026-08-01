//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"time"
)

const ownedInputSendTimeout = 5 * time.Second

const (
	OwnedInputInvalidHijack = "invalid_hijack"
	OwnedInputNoWorker      = "no_worker"
	OwnedInputSendFailed    = "send_failed"
)

// OwnedInputResult describes an exact-owner delivery attempt. LeaseExpiresAt
// is populated for a successful REST-hijack send from the same stored lease.
type OwnedInputResult struct {
	Sent           bool
	Reason         string
	LeaseExpiresAt *float64
}

type ownedInputClaim struct {
	browser           BrowserConn
	browserGeneration *uint64
	restID            string
	source            BrowserConn
}

// SendBrowserOwnedInput atomically reserves the current browser owner and
// worker, delivers msg outside the hub lock, and releases the reservation.
func (h *TermHub) SendBrowserOwnedInput(
	ctx context.Context, workerID string, browser BrowserConn, msg map[string]any,
) (bool, error) {
	result, err := h.sendOwnedInput(ctx, workerID, msg, ownedInputClaim{browser: browser, source: browser})
	return result.Sent, err
}

// SendBrowserOwnedInputAtGeneration additionally requires the ownership
// generation captured before a policy await or approval hold.
func (h *TermHub) SendBrowserOwnedInputAtGeneration(
	ctx context.Context, workerID string, browser BrowserConn, generation uint64, msg map[string]any,
) (bool, error) {
	result, err := h.sendOwnedInput(ctx, workerID, msg, ownedInputClaim{
		browser: browser, browserGeneration: &generation, source: browser,
	})
	return result.Sent, err
}

// SendBrowserOwnedInputBatchAtGeneration reserves ownership once for an
// approved command and its buffered replay, so lifecycle transitions cannot
// interleave between those sends.
func (h *TermHub) SendBrowserOwnedInputBatchAtGeneration(
	ctx context.Context, workerID string, browser BrowserConn, generation uint64, msgs []map[string]any,
) (bool, error) {
	claim := ownedInputClaim{browser: browser, browserGeneration: &generation, source: browser}
	reservation, result, err := h.reserveOwnedInput(ctx, workerID, claim)
	if err != nil || reservation == nil {
		return result.Sent, err
	}
	sendCtx, cancel := context.WithTimeout(ctx, ownedInputSendTimeout)
	defer cancel()
	var sendErr error
	for _, msg := range msgs {
		if str(msg["type"]) == "input" {
			h.Router.RecordKeystroke(browser)
		}
		if sendErr = h.Router.deliverWorker(sendCtx, reservation.Worker, reservation.IsTunnel, msg); sendErr != nil {
			break
		}
	}
	finished, err := h.finishOwnedInput(sendCtx, workerID, claim, reservation, sendErr)
	return finished.Sent, err
}

// BrowserInputFence captures a browser's current authorization generation.
func (h *TermHub) BrowserInputFence(workerID string, browser BrowserConn) (uint64, bool) {
	h.lock.Lock()
	defer h.lock.Unlock()
	st := h.registry.Get(workerID)
	if st == nil || st.WorkerWS == nil || st.HijackPending != nil {
		return 0, false
	}
	if _, registered := st.Browsers[browser]; !registered || !h.CanSendInput(st, browser) {
		return 0, false
	}
	return st.HijackOwnerGeneration, true
}

// SendRESTOwnedInput atomically reserves the exact REST hijack id and worker,
// delivers msg outside the hub lock, and releases the reservation.
func (h *TermHub) SendRESTOwnedInput(
	ctx context.Context, workerID, hijackID string, msg map[string]any,
) (OwnedInputResult, error) {
	return h.sendOwnedInput(ctx, workerID, msg, ownedInputClaim{restID: hijackID})
}

func (h *TermHub) sendOwnedInput(
	ctx context.Context, workerID string, msg map[string]any, claim ownedInputClaim,
) (OwnedInputResult, error) {
	reservation, result, err := h.reserveOwnedInput(ctx, workerID, claim)
	if err != nil || reservation == nil {
		return result, err
	}
	if claim.source != nil && str(msg["type"]) == "input" {
		h.Router.RecordKeystroke(claim.source)
	}

	sendCtx, cancel := context.WithTimeout(ctx, ownedInputSendTimeout)
	defer cancel()
	sendErr := h.Router.deliverWorker(sendCtx, reservation.Worker, reservation.IsTunnel, msg)
	return h.finishOwnedInput(sendCtx, workerID, claim, reservation, sendErr)
}

func (h *TermHub) reserveOwnedInput(
	ctx context.Context, workerID string, claim ownedInputClaim,
) (*InputSendReservation, OwnedInputResult, error) {
	for {
		h.lock.Lock()
		st := h.registry.Get(workerID)
		if st == nil {
			h.lock.Unlock()
			return nil, OwnedInputResult{Reason: claim.invalidReason()}, nil
		}
		if pending := st.InputSendPending; pending != nil {
			done := pending.Done
			h.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return nil, OwnedInputResult{}, err
			}
			continue
		}
		if !h.claimOwnsInput(st, claim) || st.HijackPending != nil {
			h.lock.Unlock()
			return nil, OwnedInputResult{Reason: claim.invalidReason()}, nil
		}
		if st.WorkerWS == nil {
			h.lock.Unlock()
			return nil, OwnedInputResult{Reason: OwnedInputNoWorker}, nil
		}
		if claim.browser != nil && h.State.IsDashboardHijackActive(st) && st.HijackOwner == claim.browser {
			expires := h.clock.Monotonic() + float64(h.Lease.DashboardHijackLeaseS())
			st.HijackOwnerExpiresAt = &expires
		}
		reservation := &InputSendReservation{
			Worker: st.WorkerWS, IsTunnel: st.IsTunnelWorker, Done: make(chan struct{}),
		}
		st.InputSendPending = reservation
		h.lock.Unlock()
		return reservation, OwnedInputResult{}, nil
	}
}

func (h *TermHub) claimOwnsInput(st *WorkerTermState, claim ownedInputClaim) bool {
	if claim.restID != "" {
		return st.HijackSession != nil &&
			st.HijackSession.HijackID == claim.restID &&
			st.HijackSession.LeaseExpiresAt > h.clock.Monotonic()
	}
	if claim.browser == nil {
		return false
	}
	if claim.browserGeneration != nil && st.HijackOwnerGeneration != *claim.browserGeneration {
		return false
	}
	if _, registered := st.Browsers[claim.browser]; !registered {
		return false
	}
	return h.CanSendInput(st, claim.browser)
}

func (h *TermHub) finishOwnedInput(
	ctx context.Context,
	workerID string,
	claim ownedInputClaim,
	reservation *InputSendReservation,
	sendErr error,
) (OwnedInputResult, error) {
	result := OwnedInputResult{}
	h.lock.Lock()
	st := h.registry.Get(workerID)
	if st != nil && st.InputSendPending == reservation {
		st.InputSendPending = nil
		if sendErr == nil && st.WorkerWS == reservation.Worker {
			result.Sent = true
			st.LastActivityAt = h.clock.Monotonic()
			if claim.restID != "" && st.HijackSession != nil && st.HijackSession.HijackID == claim.restID {
				expires := st.HijackSession.LeaseExpiresAt
				result.LeaseExpiresAt = &expires
			}
		} else if sendErr != nil && ctx.Err() == nil && st.WorkerWS == reservation.Worker {
			st.WorkerWS = nil
		}
	}
	close(reservation.Done)
	h.lock.Unlock()
	if result.Sent {
		return result, nil
	}
	result.Reason = OwnedInputSendFailed
	if ctx.Err() != nil {
		return result, sendErr
	}
	return result, nil
}

func (claim ownedInputClaim) invalidReason() string {
	if claim.restID != "" {
		return OwnedInputInvalidHijack
	}
	return "not_owner"
}

func waitInputReservation(ctx context.Context, done <-chan struct{}) error {
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

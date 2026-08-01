//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
)

const (
	OwnedInputInvalidHijack = "invalid_hijack"
	OwnedInputNoWorker      = "no_worker"
	OwnedInputSendFailed    = "send_failed"
	OwnedInputUnsupported   = "unsupported"
)

var errOwnedInputUnsupported = errors.New("worker transport does not support this input operation")

// OwnedInputResult describes an exact-owner delivery attempt. LeaseExpiresAt
// is populated for a successful REST-hijack send from the same stored lease.
type OwnedInputResult struct {
	Sent           bool
	Reason         string
	LeaseExpiresAt *float64
}

// OwnedInputBatchResult preserves partial delivery. In particular, an
// approval command that reached the worker remains delivered even if replaying
// subsequently buffered input fails.
type OwnedInputBatchResult struct {
	Delivered int
	Total     int
	Reason    string
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
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	result, err := h.sendOwnedInput(opCtx, workerID, msg, ownedInputClaim{browser: browser, source: browser})
	return result.Sent, err
}

// SendBrowserOwnedInputAtGeneration additionally requires the ownership
// generation captured before a policy await or approval hold.
func (h *TermHub) SendBrowserOwnedInputAtGeneration(
	ctx context.Context, workerID string, browser BrowserConn, generation uint64, msg map[string]any,
) (bool, error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	result, err := h.sendOwnedInput(opCtx, workerID, msg, ownedInputClaim{
		browser: browser, browserGeneration: &generation, source: browser,
	})
	return result.Sent, err
}

// SendBrowserOwnedInputBatchAtGeneration reserves ownership once for an
// approved command and its buffered replay, so lifecycle transitions cannot
// interleave between those sends.
func (h *TermHub) SendBrowserOwnedInputBatchAtGeneration(
	ctx context.Context, workerID string, browser BrowserConn, generation uint64, msgs []map[string]any,
) (OwnedInputBatchResult, error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	batch := OwnedInputBatchResult{Total: len(msgs)}
	claim := ownedInputClaim{browser: browser, browserGeneration: &generation, source: browser}
	reservation, result, err := h.reserveOwnedInput(opCtx, workerID, claim)
	if err != nil || reservation == nil {
		batch.Reason = result.Reason
		return batch, err
	}
	return h.deliverBrowserOwnedInputBatch(opCtx, workerID, claim, reservation, msgs, batch)
}

// SendApprovedBrowserInputAtGeneration reserves the worker before unparking
// the browser and taking its replay buffer. Fresh input therefore either joins
// the replay buffer or waits behind the same reservation; it cannot overtake
// the approved command/replay batch.
func (h *TermHub) SendApprovedBrowserInputAtGeneration(
	ctx context.Context, workerID string, browser BrowserConn, generation uint64, command map[string]any,
) (OwnedInputBatchResult, error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	claim := ownedInputClaim{browser: browser, browserGeneration: &generation, source: browser}
	reservation, result, err := h.reserveOwnedInput(opCtx, workerID, claim)
	if err != nil || reservation == nil {
		h.releaseParkedBrowser(&ApprovalRequest{OriginBrowser: browser}, false)
		return OwnedInputBatchResult{Total: 1, Reason: result.Reason}, err
	}
	h.lock.Lock()
	delete(h.pausedBrowsers, browser)
	replay := h.holdBuffers[browser]
	delete(h.holdBuffers, browser)
	h.lock.Unlock()
	msgs := []map[string]any{command}
	if replay != "" {
		msgs = append(msgs, map[string]any{"type": "input", "data": replay, "ts": h.clock.Wall()})
	}
	return h.deliverBrowserOwnedInputBatch(opCtx, workerID, claim, reservation, msgs,
		OwnedInputBatchResult{Total: len(msgs)})
}

func (h *TermHub) deliverBrowserOwnedInputBatch(
	ctx context.Context,
	workerID string,
	claim ownedInputClaim,
	reservation *InputSendReservation,
	msgs []map[string]any,
	batch OwnedInputBatchResult,
) (OwnedInputBatchResult, error) {
	var sendErr error
	for _, msg := range msgs {
		if claim.source != nil && str(msg["type"]) == "input" {
			h.Router.RecordKeystroke(claim.source)
		}
		if sendErr = h.Router.deliverWorker(ctx, reservation.Worker, reservation.IsTunnel, msg); sendErr != nil {
			break
		}
		batch.Delivered++
	}
	finished, err := h.finishOwnedInput(ctx, workerID, claim, reservation, sendErr)
	if batch.Delivered == batch.Total && finished.Sent {
		return batch, nil
	}
	batch.Reason = finished.Reason
	return batch, err
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
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	return h.sendOwnedInput(opCtx, workerID, msg, ownedInputClaim{restID: hijackID})
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

	sendErr := error(nil)
	if reservation.IsTunnel && !tunnelSupportsOwnedMessage(msg) {
		sendErr = errOwnedInputUnsupported
	} else {
		sendErr = h.Router.deliverWorker(ctx, reservation.Worker, reservation.IsTunnel, msg)
	}
	return h.finishOwnedInput(ctx, workerID, claim, reservation, sendErr)
}

func tunnelSupportsOwnedMessage(msg map[string]any) bool {
	msgType := str(msg["type"])
	return msgType == "input" || httpInspectControlTypes[msgType]
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
		if pending := st.LifecyclePending; pending != nil {
			done := pending.Done
			h.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return nil, OwnedInputResult{}, err
			}
			continue
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
			Worker: st.WorkerWS, WorkerGeneration: st.WorkerGeneration,
			IsTunnel: st.IsTunnelWorker, Done: make(chan struct{}),
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
		unsupported := errors.Is(sendErr, errOwnedInputUnsupported)
		if sendErr == nil && st.WorkerWS == reservation.Worker && st.WorkerGeneration == reservation.WorkerGeneration {
			result.Sent = true
			st.LastActivityAt = h.clock.Monotonic()
			if claim.restID != "" && st.HijackSession != nil && st.HijackSession.HijackID == claim.restID {
				expires := st.HijackSession.LeaseExpiresAt
				result.LeaseExpiresAt = &expires
			}
		} else if sendErr != nil && !unsupported && ctx.Err() == nil && st.WorkerWS == reservation.Worker && st.WorkerGeneration == reservation.WorkerGeneration {
			st.WorkerWS = nil
		}
	}
	close(reservation.Done)
	h.lock.Unlock()
	if result.Sent {
		return result, nil
	}
	if errors.Is(sendErr, errOwnedInputUnsupported) {
		result.Reason = OwnedInputUnsupported
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
	acknowledgeReservationWait(ctx)
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

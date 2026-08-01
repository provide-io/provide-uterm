//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"errors"
	"strings"
)

// ErrApprovalOwnershipInvalid means the submitting browser's capability fence
// no longer authorizes worker input.
var ErrApprovalOwnershipInvalid = errors.New("approval input ownership is no longer valid")
var ErrApprovalDuplicateID = errors.New("approval request id already exists")

// PendingApprovals returns a snapshot slice of every request still PENDING.
// The store lock is held only for the snapshot; callers iterate the returned
// copy. Port of the “hub.approval_store._requests.values()“ iteration the
// Python “GET /api/approvals“ route performs (which this exposes as a proper
// accessor rather than reaching the private dict).
func (s *InMemoryApprovalStore) PendingApprovals() []*ApprovalRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*ApprovalRequest
	for _, req := range s.requests {
		if req.Status == ApprovalPending {
			out = append(out, cloneApprovalRequest(req))
		}
	}
	return out
}

// IsNoOpPolicyGate reports whether the hub's input policy gate is the default
// allow-everything gate. Port of browser_handlers._is_noop_policy_gate: when
// true the browser input path forwards directly without approval interception.
func (h *TermHub) IsNoOpPolicyGate() bool {
	_, ok := h.policyGate.(NoOpPolicyGate)
	return ok
}

// IsBrowserParked reports whether ws is currently held awaiting an approval
// decision (Python “ws in hub._paused_browsers“).
func (h *TermHub) IsBrowserParked(ws BrowserConn) bool {
	h.lock.Lock()
	defer h.lock.Unlock()
	return h.pausedBrowsers[ws]
}

// HoldBrowserInput appends data to ws's hold buffer while it is parked,
// returning tooLong when the buffer would exceed MaxBufferChars (in which case
// nothing is stored). Port of the “ws in hub._paused_browsers“ branch of
// _handle_input.
func (h *TermHub) HoldBrowserInput(ws BrowserConn, data string) (tooLong bool) {
	h.lock.Lock()
	defer h.lock.Unlock()
	newHold := h.holdBuffers[ws] + data
	if len(newHold) > h.maxBufferChars {
		return true
	}
	h.holdBuffers[ws] = newHold
	return false
}

// TryHoldBrowserInput atomically checks whether ws is still parked and, when
// it is, appends data to the hold buffer. held=false tells the caller that an
// approval concurrently unparked the browser and normal fenced delivery must
// continue instead of dropping the input.
func (h *TermHub) TryHoldBrowserInput(ws BrowserConn, data string) (held, tooLong bool) {
	h.lock.Lock()
	defer h.lock.Unlock()
	if !h.pausedBrowsers[ws] {
		return false, false
	}
	newHold := h.holdBuffers[ws] + data
	if len(newHold) > h.maxBufferChars {
		return true, true
	}
	h.holdBuffers[ws] = newHold
	return true, false
}

// InterceptBrowserInput runs the input policy gate for a browser input frame,
// building the policy context first. Port of the gate.intercept_input call in
// _handle_input.
func (h *TermHub) InterceptBrowserInput(
	ctx context.Context, workerID string, ws BrowserConn, data string,
) (PolicyDecision, error) {
	action := "input"
	pc, err := h.preparePolicyContext(ctx, ws, workerID, &action)
	if err != nil {
		return PolicyDecision{}, err
	}
	return h.policyGate.InterceptInput(ctx, data, pc)
}

// ParkBrowserForApproval registers a pending approval for command, parks ws
// (further input is buffered by HoldBrowserInput until the decision), and
// broadcasts an approval_pending frame to every browser of workerID. Returns
// the request id. Port of the “decision.action == "hold"“ branch of
// _handle_input.
func (h *TermHub) ParkBrowserForApproval(
	ctx context.Context, workerID string, ws BrowserConn, command string, decision PolicyDecision,
	ownershipGeneration ...uint64,
) (string, error) {
	requestID := ""
	if decision.RequestID != nil && *decision.RequestID != "" {
		requestID = *decision.RequestID
	} else {
		requestID = h.newID()
	}
	now := h.clock.Wall()
	expiresAt := now + float64(decision.TimeoutS)

	submitter := "anonymous"
	if s := browserPrincipalSubjectID(ws); s != nil {
		submitter = *s
	}
	h.lock.Lock()
	st := h.registry.Get(workerID)
	generation := uint64(0)
	if st != nil {
		generation = st.HijackOwnerGeneration
	}
	if len(ownershipGeneration) != 0 {
		generation = ownershipGeneration[0]
	}
	if st == nil || st.HijackOwnerGeneration != generation || !h.CanSendInput(st, ws) {
		h.lock.Unlock()
		return "", ErrApprovalOwnershipInvalid
	}
	if !h.Approvals.Add(&ApprovalRequest{
		ID:               requestID,
		WorkerID:         workerID,
		SubmitterID:      submitter,
		Command:          command,
		Status:           ApprovalPending,
		CreatedAt:        now,
		ExpiresAt:        expiresAt,
		OriginBrowser:    ws,
		OriginGeneration: generation,
	}) {
		h.lock.Unlock()
		return "", ErrApprovalDuplicateID
	}
	h.pausedBrowsers[ws] = true
	h.lock.Unlock()

	err := h.Router.Broadcast(ctx, workerID, map[string]any{
		"type":       "approval_pending",
		"command":    command,
		"request_id": requestID,
		"expires_at": expiresAt,
	})
	return requestID, err
}

// ResolveApproval resolves a pending approval exactly once. It claims the
// request (idempotent — a second call is a no-op returning false), then:
//   - on approve, re-injects the held command to the worker as an
//     {"type":"input","data":command,"ts":now} frame;
//   - on reject, broadcasts a red "[REJECTED] Command '…' blocked by Admin."
//     terminal message to the browsers;
//
// then releases any parked browsers (replaying their hold buffers on approve)
// and broadcasts an approval_resolved frame. Port of
// core_orchestration.resolve_approval (the non-fanout browser/hold path).
//
// resolver is the principal that made the decision; it is used only for the
// audit log line (the self-approval gate lives in the REST route, mirroring
// the Python approve_command handler). reason, when non-nil, is appended to the
// rejection message.
func (h *TermHub) ResolveApproval(
	ctx context.Context, requestID string, approve bool, reason *string, resolver *Principal,
) (bool, error) {
	req := h.Approvals.Get(requestID)
	if req == nil {
		return false, nil
	}
	workerID := req.WorkerID
	command := req.Command

	status := ApprovalRejected
	if approve {
		status = ApprovalApproved
	}
	if !h.Approvals.ClaimRevision(requestID, req.Revision, status) {
		// Already resolved by a concurrent/prior call: idempotent no-op.
		return false, nil
	}
	if approve {
		batch, err := h.SendApprovedBrowserInputAtGeneration(
			ctx, workerID, req.OriginBrowser, req.OriginGeneration,
			map[string]any{"type": "input", "data": command, "ts": h.clock.Wall()},
		)
		if batch.Delivered == 0 {
			if !h.Approvals.SetStatusRevision(requestID, req.Revision, ApprovalRefused) {
				return true, nil
			}
			_ = h.Router.Broadcast(ctx, workerID, map[string]any{
				"type": "approval_resolved", "outcome": "refused", "request_id": requestID,
			})
			h.logger.Info("approval_resolved",
				"request_id", requestID, "worker_id", workerID,
				"approved", false, "outcome", "refused", "resolver", resolverSubject(resolver))
			if err != nil {
				return true, err
			}
			return true, ErrApprovalOwnershipInvalid
		}
		if batch.Delivered < batch.Total {
			h.logger.Info("approval_replay_failed",
				"request_id", requestID, "worker_id", workerID,
				"delivered", batch.Delivered, "total", batch.Total, "reason", batch.Reason)
		}
	} else if err := h.Router.Broadcast(ctx, workerID, map[string]any{
		"type": "term", "data": rejectMessage(command, reason),
	}); err != nil {
		return true, err
	}

	if !approve {
		h.releaseParkedBrowser(req, false)
	}
	// Re-check the opaque record identity before publishing/auditing a terminal
	// outcome. A long-running resolver may outlive pruning and ID reuse.
	if !h.Approvals.SetStatusRevision(requestID, req.Revision, status) {
		return true, nil
	}

	outcome := "rejected"
	if approve {
		outcome = "approved"
	}
	if err := h.Router.Broadcast(ctx, workerID, map[string]any{
		"type": "approval_resolved", "outcome": outcome, "request_id": requestID,
	}); err != nil {
		return true, err
	}
	h.logger.Info("approval_resolved",
		"request_id", requestID, "worker_id", workerID,
		"approved", approve, "outcome", outcome, "resolver", resolverSubject(resolver))
	return true, nil
}

// releaseParkedBrowsers unparks every parked browser of workerID and, on
// approve, replays each browser's buffered hold input to the worker. Port of
// the paused-browser release loop in resolve_approval.
//
// Deviation: the Python replay re-runs the buffered keystrokes through the full
// browser input handler (re-applying the policy gate / command splitter); this
// port re-injects the buffered bytes directly as a worker input frame, which is
// sufficient for the interop hold/resume behaviour.
func (h *TermHub) releaseParkedBrowser(req *ApprovalRequest, approve bool) string {
	h.lock.Lock()
	defer h.lock.Unlock()
	ws := req.OriginBrowser
	delete(h.pausedBrowsers, ws)
	buf := h.holdBuffers[ws]
	delete(h.holdBuffers, ws)
	if approve {
		return buf
	}
	return ""
}

// rejectMessage builds the red terminal rejection banner. Byte-identical to the
// Python resolve_approval deny branch (command stripped of surrounding
// whitespace; optional yellow reason clause).
func rejectMessage(command string, reason *string) string {
	msg := "\r\x1b[31m[REJECTED] Command '" + strings.TrimSpace(command) + "' blocked by Admin.\x1b[0m"
	if reason != nil && *reason != "" {
		msg += " \x1b[33mReason: " + *reason + "\x1b[0m"
	}
	return msg + "\r"
}

// resolverSubject returns the resolver's subject id for the audit log, or
// "unknown" when no resolver was supplied.
func resolverSubject(resolver *Principal) string {
	if resolver == nil {
		return "unknown"
	}
	return resolver.SubjectID
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// TermHub implements [LeaseHub] so the wave-A lease manager can dispatch its
// cross-cutting side effects back through the composing hub, exactly as the
// Python lease manager reaches its _LeaseHubCallbacks. Each method is a thin
// delegator to the owning service.

// IsHijacked implements [LeaseHub].
func (h *TermHub) IsHijacked(st *WorkerTermState) bool { return h.State.IsHijacked(st) }

// IsDashboardHijackActive implements [LeaseHub].
func (h *TermHub) IsDashboardHijackActive(st *WorkerTermState) bool {
	return h.State.IsDashboardHijackActive(st)
}

// HasValidRESTLease implements [LeaseHub].
func (h *TermHub) HasValidRESTLease(st *WorkerTermState) bool { return h.State.HasValidRESTLease(st) }

// CanSendInput implements [LeaseHub].
func (h *TermHub) CanSendInput(st *WorkerTermState, ws BrowserConn) bool {
	return h.Presence.CanSendInput(st, ws)
}

// Metric implements [LeaseHub].
func (h *TermHub) Metric(name string, value int) { h.State.Metric(name, value) }

// NotifyHijackChanged implements [LeaseHub].
func (h *TermHub) NotifyHijackChanged(workerID string, enabled bool, owner *string) {
	h.State.NotifyHijackChanged(workerID, enabled, owner)
}

// SendWorker implements [LeaseHub] (and is the public worker-send facade).
func (h *TermHub) SendWorker(ctx context.Context, workerID string, msg map[string]any) (bool, error) {
	return h.Router.SendWorker(ctx, workerID, msg, nil)
}

// BroadcastHijackState implements [LeaseHub] (and is the public facade).
func (h *TermHub) BroadcastHijackState(ctx context.Context, workerID string) error {
	return h.Router.BroadcastHijackState(ctx, workerID)
}

// AppendEvent implements [LeaseHub]: append an event, discarding the returned
// row (the lease manager only needs the error).
func (h *TermHub) AppendEvent(ctx context.Context, workerID, eventType string) error {
	_, err := h.Router.AppendEvent(ctx, workerID, eventType, nil)
	return err
}

// PruneIfIdle implements [LeaseHub] (and is the public facade).
func (h *TermHub) PruneIfIdle(ctx context.Context, workerID string) error {
	return h.Router.PruneIfIdle(ctx, workerID)
}

// RecheckAndResume implements [LeaseHub], forwarding to the lease manager (the
// indirection matches the Python hub shim so callers can intercept it).
func (h *TermHub) RecheckAndResume(ctx context.Context, workerID string, now float64) error {
	return h.Lease.RecheckAndResume(ctx, workerID, now)
}

// -- Internal cross-cutting helpers used by the wave-B services ---------------

// get returns (creating if needed) the worker state for workerID. Port of
// TermHub._get.
func (h *TermHub) get(workerID string) *WorkerTermState { return h.State.GetOrCreate(workerID) }

// preparePolicyContext builds a policy context for ws + workerID.
func (h *TermHub) preparePolicyContext(
	ctx context.Context, ws BrowserConn, workerID string, action *string,
) (PolicyContext, error) {
	return h.State.PreparePolicyContext(ctx, ws, workerID, action)
}

// resolveRoleForBrowser resolves ws's role via the state store. Port of
// TermHub._resolve_role_for_browser.
func (h *TermHub) resolveRoleForBrowser(ctx context.Context, ws BrowserConn, workerID string) (string, error) {
	return h.State.ResolveRoleForBrowser(ctx, ws, workerID)
}

// emitTelemetry emits a lifecycle event to the configured sink. Strictly
// additive and fail-open: no sink or a raising sink silently returns. Port of
// core_orchestration.emit_telemetry.
func (h *TermHub) emitTelemetry(
	ctx context.Context, eventType, workerID string, principal, role *string, metadata map[string]any,
) {
	if h.telemetrySink == nil {
		return
	}
	if metadata == nil {
		metadata = map[string]any{}
	}
	evt := TelemetryEvent{
		EventType: eventType,
		WorkerID:  workerID,
		Principal: principal,
		Role:      role,
		Metadata:  metadata,
		Timestamp: h.clock.Wall(),
	}
	_ = h.telemetrySink.Emit(ctx, evt) //nolint:errcheck // fail-open: telemetry must never propagate
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// broadcastSendTimeoutS is the per-browser send deadline in broadcast() /
// send_hijack_state_to(). A viewer whose receive window is stalled is treated
// as dead and pruned rather than head-of-line-blocking the fan-out. Port of
// router_impl._BROADCAST_SEND_TIMEOUT_S.
const broadcastSendTimeoutS = 5.0

// contentEventTypes are the event types whose payloads carry terminal output
// and would be redacted at write time. Port of _REDACTED_EVENT_TYPES.
var contentEventTypes = map[string]bool{"term": true, "snapshot": true, "analysis": true}

// MessageRouter owns the outbound-frame plumbing: broadcasts, worker sends,
// hijack-state notifications, the event ring buffer, and the behavioral
// keystroke ring buffers. Port of
// provide.uterm.server.bridge.hub.router_impl.MessageRouter composed with its
// router_broadcast / router_behavioral bodies.
//
// It holds a back reference to the composing [TermHub] and uses the hub's
// shared mutex for every worker-state block, preserving the Python lock
// semantics verbatim.
type MessageRouter struct {
	hub *TermHub

	keystrokeMu sync.Mutex
	keystrokes  map[BrowserConn][]float64
}

// newMessageRouter builds a router bound to hub.
func newMessageRouter(hub *TermHub) *MessageRouter {
	return &MessageRouter{hub: hub, keystrokes: map[BrowserConn][]float64{}}
}

// IdleCandidate is one entry returned by [MessageRouter.GetIdleCandidates].
type IdleCandidate struct {
	WorkerID       string
	LastActivityAt float64
}

// AppendEvent appends a timestamped event to the worker's event ring buffer and
// returns it. Port of append_event.
//
// Deviation: the Python path redacts content events (term/snapshot/analysis)
// with the server-default ruleset before storing. The regex redaction engine is
// outside the wave-B port, so events are stored unredacted here (the live
// broadcast path is likewise gate-driven — see the package deviations note).
// The term-data char cap is preserved (rune-based, matching Python str slicing).
func (r *MessageRouter) AppendEvent(
	ctx context.Context, workerID, eventType string, data map[string]any,
) (map[string]any, error) {
	_ = ctx
	hub := r.hub
	payload := data
	if payload == nil {
		payload = map[string]any{}
	}
	if eventType == "term" {
		if raw, ok := payload["data"].(string); ok {
			cap := hub.maxEventDataChars
			if runeLen(raw) > cap {
				trimmed := map[string]any{}
				for k, v := range payload {
					trimmed[k] = v
				}
				trimmed["data"] = runeSlice(raw, cap)
				payload = trimmed
			}
		}
	}

	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil {
		hub.lock.Unlock()
		return map[string]any{"seq": 0, "ts": hub.clock.Wall(), "type": eventType, "data": payload}, nil
	}
	st.EventSeq++
	evt := map[string]any{"seq": st.EventSeq, "ts": hub.clock.Wall(), "type": eventType, "data": payload}
	st.Events = append(st.Events, evt)
	if len(st.Events) > hub.eventDequeMaxlen {
		st.Events = st.Events[len(st.Events)-hub.eventDequeMaxlen:]
	}
	st.MinEventSeq = coerceSeq(st.Events[0])
	hub.lock.Unlock()

	if hub.eventBus != nil {
		hub.eventBus.Enqueue(workerID, evt)
	}
	return evt, nil
}

// PruneIfIdle removes worker state when no connections or leases remain. Port
// of prune_if_idle.
func (r *MessageRouter) PruneIfIdle(_ context.Context, workerID string) error {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	if st == nil {
		return nil
	}
	if st.WorkerWS == nil && len(st.Browsers) == 0 && st.HijackOwner == nil && st.HijackSession == nil {
		hub.registry.Pop(workerID)
		hub.logger.Debug("pruned idle worker", "worker_id", workerID)
	}
	return nil
}

// HijackStateMsgFor builds a hijack_state frame for ws, setting owner="me" when
// ws holds the dashboard lease. Port of hijack_state_msg_for.
func (r *MessageRouter) HijackStateMsgFor(_ context.Context, workerID string, ws BrowserConn) frames.HijackStateFrame {
	hub := r.hub
	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil {
		hub.lock.Unlock()
		return frames.MakeHijackStateFrame(false, nil, nil, "hijack")
	}
	isDashboard := hub.State.IsDashboardHijackActive(st)
	isRest := hub.State.HasValidRESTLease(st)
	isH := isDashboard || isRest
	inputMode := st.InputMode
	var leaseExpiresAt *float64
	if isRest && st.HijackSession != nil {
		leaseExpiresAt = f64p(st.HijackSession.LeaseExpiresAt)
	} else {
		leaseExpiresAt = st.HijackOwnerExpiresAt
	}
	var owner *string
	switch {
	case isDashboard && st.HijackOwner == ws:
		owner = strp("me")
	case isDashboard || isRest:
		owner = strp("other")
	}
	hub.lock.Unlock()
	return frames.MakeHijackStateFrame(isH, owner, monoToWall(hub.clock, leaseExpiresAt), inputMode)
}

// SetInputMode sets the worker input mode under lock, rejecting a switch to
// "open" while a hijack is active. Port of set_input_mode. Returns (ok, reason).
func (r *MessageRouter) SetInputMode(ctx context.Context, workerID, mode string) (bool, string, error) {
	hub := r.hub
	hub.lock.Lock()
	st := hub.registry.Get(workerID)
	if st == nil {
		hub.lock.Unlock()
		return false, "not_found", nil
	}
	if mode == InputModeOpen && hub.State.IsHijacked(st) {
		hub.lock.Unlock()
		return false, "active_hijack", nil
	}
	st.InputMode = mode
	// Every caller of this is an authenticated route — the session routes and
	// the worker-control route, which requires session.control.mode. So reaching
	// here means somebody decided the mode, and a later worker_hello may raise
	// it but never lower it back. See WorkerTermState.InputModeSetByOperator.
	st.InputModeSetByOperator = true
	hub.lock.Unlock()

	if err := r.Broadcast(ctx, workerID, map[string]any{
		"type": "input_mode_changed", "input_mode": mode, "ts": hub.clock.Wall(),
	}); err != nil {
		return false, "", err
	}
	if err := r.BroadcastHijackState(ctx, workerID); err != nil {
		return false, "", err
	}
	return true, "", nil
}

// GetIdleCandidates returns (worker_id, last_activity_at) for workers with no
// browsers idle beyond timeoutS. Port of get_idle_candidates.
func (r *MessageRouter) GetIdleCandidates(_ context.Context, timeoutS float64) []IdleCandidate {
	hub := r.hub
	now := hub.clock.Monotonic()
	hub.lock.Lock()
	defer hub.lock.Unlock()
	var out []IdleCandidate
	for _, wid := range hub.registry.Keys() {
		st := hub.registry.Get(wid)
		if st != nil && len(st.Browsers) == 0 && (now-st.LastActivityAt) > timeoutS {
			out = append(out, IdleCandidate{WorkerID: wid, LastActivityAt: st.LastActivityAt})
		}
	}
	return out
}

// SetBrowserRole updates the role for ws in workerID's browser set. Port of
// set_browser_role.
func (r *MessageRouter) SetBrowserRole(_ context.Context, workerID string, ws BrowserConn, role string) {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	if st != nil {
		if _, ok := st.Browsers[ws]; ok {
			st.Browsers[ws] = role
		}
	}
}

// TryReclaimHijack acquires hijack ownership for ws when the session is
// unhijacked and in a reclaimable state. Port of try_reclaim_hijack.
func (r *MessageRouter) TryReclaimHijack(ctx context.Context, workerID string, ws BrowserConn) bool {
	hub := r.hub
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if st != nil && st.InputSendPending != nil {
			done := st.InputSendPending.Done
			hub.lock.Unlock()
			if waitInputReservation(ctx, done) != nil {
				return false
			}
			continue
		}
		if st != nil && st.WorkerWS != nil && st.InputMode != InputModeOpen &&
			st.HijackOwner == nil && !hub.State.IsHijacked(st) {
			exp := hub.clock.Monotonic() + float64(hub.Lease.DashboardHijackLeaseS())
			st.setDashboardOwner(ws, &exp)
			hub.lock.Unlock()
			return true
		}
		hub.lock.Unlock()
		return false
	}
}

// GetWorkerBrowserRole returns the role assigned to ws for workerID. Port of
// get_worker_browser_role. ok is false when the worker or ws is unknown.
func (r *MessageRouter) GetWorkerBrowserRole(_ context.Context, workerID string, ws BrowserConn) (string, bool) {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	if st == nil {
		return "", false
	}
	role, ok := st.Browsers[ws]
	return role, ok
}

// GetLastSnapshot returns the most recent snapshot for workerID, redacted for
// recipient when an output-policy gate is active. Port of get_last_snapshot.
func (r *MessageRouter) GetLastSnapshot(
	ctx context.Context, workerID string, recipient BrowserConn,
) (map[string]any, error) {
	hub := r.hub
	hub.lock.Lock()
	var snapshot map[string]any
	if st := hub.registry.Get(workerID); st != nil {
		snapshot = st.LastSnapshot
	}
	hub.lock.Unlock()
	if snapshot == nil || recipient == nil || hub.outputPolicyGate == nil {
		return snapshot, nil
	}
	return r.RedactSnapshotForRecipient(ctx, workerID, snapshot, recipient)
}

// RedactSnapshotForRecipient returns a recipient-role-redacted COPY of
// snapshot. Port of redact_snapshot_for_recipient. Returns snapshot unchanged
// when the gate is inactive, yields no rules, or no [Redactor] is wired.
func (r *MessageRouter) RedactSnapshotForRecipient(
	ctx context.Context, workerID string, snapshot map[string]any, recipient BrowserConn,
) (map[string]any, error) {
	hub := r.hub
	if hub.outputPolicyGate == nil {
		return snapshot, nil
	}
	pc, err := hub.preparePolicyContext(ctx, recipient, workerID, strp("output"))
	if err != nil {
		return nil, err
	}
	rules, err := hub.outputPolicyGate.GetRedactionRules(ctx, pc)
	if err != nil {
		return nil, err
	}
	if len(rules) == 0 || hub.redactor == nil {
		return snapshot, nil
	}
	toRedact := map[string]any{}
	for k, v := range snapshot {
		toRedact[k] = v
	}
	toRedact["type"] = "snapshot"
	return hub.redactor(toRedact, rules), nil
}

// BrowserCount returns the number of browsers connected for workerID. Port of
// browser_count.
func (r *MessageRouter) BrowserCount(_ context.Context, workerID string) int {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	if st == nil {
		return 0
	}
	return len(st.Browsers)
}

// BrowserCountTotal returns the total browsers connected across all workers.
// Port of browser_count_total.
func (r *MessageRouter) BrowserCountTotal(_ context.Context) int {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	total := 0
	for _, st := range hub.registry.All() {
		total += len(st.Browsers)
	}
	return total
}

// GetRecentEvents returns the most recent events for workerID (up to limit,
// clamped to 1..500). Port of get_recent_events.
func (r *MessageRouter) GetRecentEvents(_ context.Context, workerID string, limit int) []map[string]any {
	hub := r.hub
	hub.lock.Lock()
	defer hub.lock.Unlock()
	st := hub.registry.Get(workerID)
	if st == nil {
		return []map[string]any{}
	}
	n := clampMin(limit, 1)
	if n > 500 {
		n = 500
	}
	if len(st.Events) <= n {
		return append([]map[string]any(nil), st.Events...)
	}
	return append([]map[string]any(nil), st.Events[len(st.Events)-n:]...)
}

// runeLen returns the number of Unicode code points in s (Python len(str)).
func runeLen(s string) int { return len([]rune(s)) }

// runeSlice returns the first n code points of s (Python s[:n]).
func runeSlice(s string, n int) string {
	r := []rune(s)
	if n >= len(r) {
		return s
	}
	return string(r[:n])
}

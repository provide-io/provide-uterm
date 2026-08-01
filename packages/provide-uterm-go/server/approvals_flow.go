//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// browserInputGated processes a browser input frame with the input-approval
// pipeline. Port of browser_handlers._handle_input (frame-level subset):
//
//   - a parked browser (awaiting an approval decision) has its keystrokes
//     buffered, not forwarded;
//   - the no-op policy gate forwards directly (fast path);
//   - otherwise the policy gate intercepts: "hold" registers a pending approval
//     and parks the browser (broadcasting approval_pending); "deny" replies with
//     an error; "allow" forwards to the worker.
//
// The lease/permission gate (prepare_browser_input) runs first: a non-owner or
// expired-lease browser has its input dropped before any policy work.
//
// Deviation: the Python handler buffers partial keystrokes into a full command
// and splits it before interception; this port intercepts the raw input frame
// data directly (the frame IS the command). This keeps the interop
// hold→approve/reject behaviour intact.
func (s *Server) browserInputGated(ctx context.Context, workerID string, bc *browserConn, msg map[string]any) {
	data := stringField(msg, "data")
	if data == "" {
		return
	}

	// Parked browser: buffer further input until the approval resolves.
	if s.deps.Hub.IsBrowserParked(bc) {
		if s.deps.Hub.HoldBrowserInput(bc, data) {
			s.writeFrame(ctx, bc, frames.MakeErrorFrame("Input too long."))
		}
		return
	}

	if len(data) > s.deps.Hub.MaxInputChars() {
		s.writeFrame(ctx, bc, frames.MakeErrorFrame("Input too long."))
		return
	}
	generation, allowed := s.deps.Hub.BrowserInputFence(workerID, bc)
	if !allowed {
		return
	}

	// No-op policy gate → forward directly (no interception overhead).
	if s.deps.Hub.IsNoOpPolicyGate() {
		_, _ = s.deps.Hub.SendBrowserOwnedInputAtGeneration(ctx, workerID, bc, generation,
			map[string]any{"type": "input", "data": data, "ts": s.clock.Wall()})
		return
	}

	decision, err := s.deps.Hub.InterceptBrowserInput(ctx, workerID, bc, data)
	if err != nil {
		s.logger.Debug("intercept_input_failed", "worker_id", workerID, "error", err)
		return
	}
	switch decision.Action {
	case "hold":
		if _, perr := s.deps.Hub.ParkBrowserForApproval(ctx, workerID, bc, data, decision, generation); perr != nil {
			s.logger.Debug("park_for_approval_failed", "worker_id", workerID, "error", perr)
		}
	case "allow":
		_, _ = s.deps.Hub.SendBrowserOwnedInputAtGeneration(ctx, workerID, bc, generation,
			map[string]any{"type": "input", "data": data, "ts": s.clock.Wall()})
	default: // "deny"
		s.writeFrame(ctx, bc, frames.MakeErrorFrame("Command blocked by policy: "+data))
	}
}

// sendBrowserInput forwards keystrokes to the worker, recording the keystroke as
// this browser's input.
//
// The lease/permission gate runs here, at the worker-forward point: a non-owner
// or expired-lease browser (touch-if-owner lease check + can-send permission
// fails) has its keystroke dropped and never reaches the worker. Port of
// prepare_browser_input applied "before forwarding a browser input to the
// worker" — it also refreshes the dashboard lease when this browser owns it.
//
// Deviation from the Python _handle_input, which runs prepare_browser_input
// before the policy gate (so a denied browser cannot even trigger an approval
// hold): gating at the forward point keeps the approval-hold pipeline intact
// (a held command is a policy decision, not a direct worker forward) while still
// dropping every ungated/approved keystroke a non-owner would push to the worker.
func (s *Server) sendBrowserInput(ctx context.Context, workerID string, bc *browserConn, data string) {
	_, _ = s.deps.Hub.SendBrowserOwnedInput(ctx, workerID, bc,
		map[string]any{"type": "input", "data": data, "ts": s.clock.Wall()})
}

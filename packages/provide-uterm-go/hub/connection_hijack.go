//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// DisconnectWorker programmatically disconnects the worker WS and clears any
// active hijack. Port of connection_hijack.disconnect_worker. Returns true when
// a worker was connected. The inter-step hooks (broadcast, notify,
// hijack-state, prune) dispatch through the hub facade.
func (c *ConnectionManager) DisconnectWorker(ctx context.Context, workerID string) (bool, error) {
	hub := c.hub
	var ws WorkerWS
	wasHijacked := false
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if st == nil || st.WorkerWS == nil {
			hub.lock.Unlock()
			return false, nil
		}
		if pending := st.InputSendPending; pending != nil {
			done := pending.Done
			hub.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return false, err
			}
			continue
		}
		ws = st.WorkerWS
		st.WorkerWS = nil
		wasHijacked = st.HijackSession != nil || st.HijackOwner != nil
		st.HijackSession = nil
		st.clearDashboardOwner()
		hub.lock.Unlock()
		break
	}

	if closer, ok := ws.(WorkerCloser); ok {
		if err := closer.Close(ctx); err != nil {
			hub.logger.Debug("disconnect_worker close error", "worker_id", workerID, "error", err)
		}
	}

	m, err := frameToMap(frames.MakeWorkerDisconnectedFrame(workerID, 0))
	if err != nil {
		return false, err
	}
	if err := hub.Broadcast(ctx, workerID, m); err != nil {
		return false, err
	}
	if wasHijacked {
		hub.NotifyHijackChanged(workerID, false, nil)
		if err := hub.BroadcastHijackState(ctx, workerID); err != nil {
			return false, err
		}
	}
	if hub.eventBus != nil {
		c.eventBusClose(workerID)
	}
	if err := hub.PruneIfIdle(ctx, workerID); err != nil {
		return false, err
	}
	return true, nil
}

// eventBusClose closes the EventBus stream for workerID, reading the bus off the
// hub each call. Port of _event_bus_close.
func (c *ConnectionManager) eventBusClose(workerID string) {
	if bus := c.hub.eventBus; bus != nil {
		bus.CloseWorker(workerID)
	}
}

// ForceReleaseHijack forcibly clears any active hijack for workerID and sends a
// resume control frame. Port of force_release_hijack. Returns true when a hijack
// was active and cleared.
func (c *ConnectionManager) ForceReleaseHijack(ctx context.Context, workerID string) (bool, error) {
	hub := c.hub
	owner := "server-forced"
	hadHijack := false
	for {
		hub.lock.Lock()
		st := hub.registry.Get(workerID)
		if st == nil {
			hub.lock.Unlock()
			return false, nil
		}
		if pending := st.InputSendPending; pending != nil {
			done := pending.Done
			hub.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return false, err
			}
			continue
		}
		if st.HijackSession != nil {
			owner = st.HijackSession.Owner
			st.HijackSession = nil
			hadHijack = true
		}
		if hub.State.IsDashboardHijackActive(st) {
			st.clearDashboardOwner()
			hadHijack = true
		}
		hub.lock.Unlock()
		break
	}

	if !hadHijack {
		return false, nil
	}
	if _, err := hub.SendWorker(ctx, workerID, map[string]any{
		"type": "control", "action": "resume", "owner": owner, "lease_s": 0, "ts": hub.clock.Wall(),
	}); err != nil {
		return false, err
	}
	hub.NotifyHijackChanged(workerID, false, nil)
	if err := hub.BroadcastHijackState(ctx, workerID); err != nil {
		return false, err
	}
	return true, nil
}

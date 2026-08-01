//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// expireLeasesUnderLock expires stale leases under the hub lock, returning
// (restExpired, dashExpired, shouldResume, ok). ok is false when the worker is
// unknown or already idle (the Python None sentinel).
func (lm *HijackLeaseManager) expireLeasesAfterPending(
	ctx context.Context, workerID string, now float64,
) (rest, dash, resume, ok bool, lifecycle *LifecycleReservation, err error) {
	for {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if st == nil {
			lm.lock.Unlock()
			return false, false, false, false, nil, nil
		}
		lease := st.Lease()
		if lease.IsIdle() {
			lm.lock.Unlock()
			return false, false, false, false, nil, nil
		}
		if done := statePendingDone(st, true); done != nil {
			lm.lock.Unlock()
			if err := waitInputReservation(ctx, done); err != nil {
				return false, false, false, false, nil, err
			}
			continue
		}
		oldOwner := st.HijackOwner
		restExpired, dashExpired := lease.Expire(now)
		if dashExpired {
			if termHub, isTermHub := lm.hub.(*TermHub); isTermHub {
				if err := termHub.markBrowserResumeOwnerLocked(ctx, oldOwner, false); err != nil {
					lm.lock.Unlock()
					return false, false, false, false, nil, err
				}
			}
		}
		if restExpired || dashExpired {
			st.ApplyLease(lease)
		}
		shouldResume := (restExpired || dashExpired) && lease.IsIdle()
		if shouldResume {
			if termHub, isTermHub := lm.hub.(*TermHub); isTermHub {
				lifecycle = termHub.beginLifecycleLocked(st, "lease_expiry_resume")
			}
		}
		lm.lock.Unlock()
		return restExpired, dashExpired, shouldResume, true, lifecycle, nil
	}
}

func (lm *HijackLeaseManager) expireLeasesUnderLock(workerID string, now float64) (rest, dash, resume, ok bool) {
	var lifecycle *LifecycleReservation
	rest, dash, resume, ok, lifecycle, _ = lm.expireLeasesAfterPending(context.Background(), workerID, now)
	if lifecycle != nil {
		lm.hub.(*TermHub).finishLifecycle(workerID, lifecycle)
	}
	return rest, dash, resume, ok
}

// RecheckAndResume re-verifies no concurrent hijack appeared and, if clear,
// sends the resume frame and fires the hijack-changed callback. Port of
// _recheck_and_resume.
func (lm *HijackLeaseManager) RecheckAndResume(ctx context.Context, workerID string, now float64) error {
	lm.lock.Lock()
	st2 := lm.registry.Get(workerID)
	hijacked := st2 != nil && lm.hub.IsHijacked(st2)
	lm.lock.Unlock()
	if hijacked {
		return nil
	}
	if _, err := lm.hub.SendWorker(ctx, workerID, resumeFrame("lease-expired", now)); err != nil {
		return err
	}
	lm.hub.NotifyHijackChanged(workerID, false, nil)
	return nil
}

// CleanupExpired expires any stale REST or dashboard leases and emits a resume
// on full release. Port of cleanup_expired: the ordered pipeline is
// metric → recheck (which sends+notifies) → append_event(s) → broadcast → prune.
func (lm *HijackLeaseManager) CleanupExpired(ctx context.Context, workerID string) (bool, error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	now := lm.clock.Monotonic()
	restExpired, dashExpired, shouldResume, ok, lifecycle, err := lm.expireLeasesAfterPending(opCtx, workerID, now)
	if err != nil {
		return false, err
	}
	if lifecycle != nil {
		defer lm.hub.(*TermHub).finishLifecycle(workerID, lifecycle)
	}
	if !ok {
		return false, nil
	}
	if !restExpired && !dashExpired {
		return false, nil
	}
	lm.hub.Metric("hijack_lease_expiries_total", 1)
	if shouldResume {
		if lifecycle != nil {
			if _, err := lm.hub.SendWorker(opCtx, workerID, resumeFrame("lease-expired", now)); err != nil {
				return false, err
			}
			lm.hub.NotifyHijackChanged(workerID, false, nil)
		} else {
			if err := lm.hub.RecheckAndResume(opCtx, workerID, now); err != nil {
				return false, err
			}
		}
	}
	if restExpired {
		if err := lm.hub.AppendEvent(ctx, workerID, "hijack_lease_expired"); err != nil {
			return false, err
		}
		lm.logger.Info(eventHijackExpired, "worker_id", workerID, "hijack_type", "rest")
	}
	if dashExpired {
		if err := lm.hub.AppendEvent(ctx, workerID, "hijack_owner_expired"); err != nil {
			return false, err
		}
		lm.logger.Info(eventHijackExpired, "worker_id", workerID, "hijack_type", "dashboard")
	}
	if err := lm.hub.BroadcastHijackState(ctx, workerID); err != nil {
		return false, err
	}
	if err := lm.hub.PruneIfIdle(ctx, workerID); err != nil {
		return false, err
	}
	return true, nil
}

// GetRestSession runs a cleanup pass then returns the active REST session
// matching hijackID, or nil. Port of get_rest_session.
func (lm *HijackLeaseManager) GetRestSession(ctx context.Context, workerID, hijackID string) (*HijackSession, error) {
	if _, err := lm.CleanupExpired(ctx, workerID); err != nil {
		return nil, err
	}
	return lm.getRestSessionNoCleanup(workerID, hijackID), nil
}

// getRestSessionNoCleanup looks up a live REST session without the cleanup
// pass. Port of _get_rest_session_no_cleanup.
func (lm *HijackLeaseManager) getRestSessionNoCleanup(workerID, hijackID string) *HijackSession {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil {
		return nil
	}
	hs := st.HijackSession
	if hs == nil || hs.LeaseExpiresAt <= lm.clock.Monotonic() || hs.HijackID != hijackID {
		return nil
	}
	return hs
}

// GetEventsData returns the events payload for a REST hijack events endpoint.
// Port of get_events_data. The returned map carries exactly the keys
// "rows", "latest_seq", "min_event_seq", "fresh_expires".
func (lm *HijackLeaseManager) GetEventsData(
	workerID, hijackID string, hs *HijackSession, afterSeq, limit int,
) map[string]any {
	lm.lock.Lock()
	defer lm.lock.Unlock()
	st := lm.registry.Get(workerID)
	if st == nil {
		return map[string]any{
			"rows":          []map[string]any{},
			"latest_seq":    0,
			"min_event_seq": 0,
			"fresh_expires": hs.LeaseExpiresAt,
		}
	}
	rows := make([]map[string]any, 0)
	for _, evt := range st.Events {
		if coerceSeq(evt) > afterSeq {
			rows = append(rows, evt)
			if len(rows) >= limit {
				break
			}
		}
	}
	freshExpires := hs.LeaseExpiresAt
	if st.HijackSession != nil && st.HijackSession.HijackID == hijackID {
		freshExpires = st.HijackSession.LeaseExpiresAt
	}
	return map[string]any{
		"rows":          rows,
		"latest_seq":    st.EventSeq,
		"min_event_seq": st.MinEventSeq,
		"fresh_expires": freshExpires,
	}
}

// coerceSeq extracts an int "seq" from an event map, defaulting to 0. Mirrors
// int(evt.get("seq", 0)) for the int/float values the events carry.
func coerceSeq(evt map[string]any) int {
	v, ok := evt["seq"]
	if !ok {
		return 0
	}
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return 0
	}
}

// RemoveDeadBrowsers removes dead browser sockets under lock and resumes if the
// dead socket was the dashboard owner. Port of remove_dead_browsers.
func (lm *HijackLeaseManager) RemoveDeadBrowsers(ctx context.Context, workerID string, dead []BrowserConn) (bool, error) {
	opCtx, cancel := boundedOperationContext(ctx)
	defer cancel()
	notifyHijackOff := false
	var lifecycle *LifecycleReservation
	for {
		lm.lock.Lock()
		st := lm.registry.Get(workerID)
		if done := statePendingDone(st, true); done != nil {
			lm.lock.Unlock()
			if err := waitInputReservation(opCtx, done); err != nil {
				return false, err
			}
			continue
		}
		if st != nil {
			for _, ws := range dead {
				delete(st.Browsers, ws)
				if lm.hub.IsDashboardHijackActive(st) && st.HijackOwner == ws {
					if termHub, isTermHub := lm.hub.(*TermHub); isTermHub {
						if err := termHub.markBrowserResumeOwnerLocked(opCtx, ws, true); err != nil {
							lm.lock.Unlock()
							return false, err
						}
					}
					st.clearDashboardOwner()
					notifyHijackOff = !lm.hub.HasValidRESTLease(st)
					if notifyHijackOff {
						if termHub, isTermHub := lm.hub.(*TermHub); isTermHub {
							lifecycle = termHub.beginLifecycleLocked(st, "dead_browser_resume")
						}
					}
				}
				if termHub, isTermHub := lm.hub.(*TermHub); isTermHub && termHub.resumeStore != nil {
					token := termHub.wsToResumeToken[ws]
					delete(termHub.wsToResumeToken, ws)
					termHub.detachResumeTokenLocked(token)
				}
			}
		}
		lm.lock.Unlock()
		break
	}

	if notifyHijackOff && lifecycle == nil {
		// Re-check: a concurrent acquire may have written a new session.
		lm.lock.Lock()
		st2 := lm.registry.Get(workerID)
		if st2 != nil && lm.hub.IsHijacked(st2) {
			notifyHijackOff = false
		}
		lm.lock.Unlock()
	}
	if notifyHijackOff {
		if lifecycle != nil {
			defer lm.hub.(*TermHub).finishLifecycle(workerID, lifecycle)
		}
		if _, err := lm.hub.SendWorker(opCtx, workerID, resumeFrame("dead-socket", lm.clock.Wall())); err != nil {
			return false, err
		}
		lm.hub.NotifyHijackChanged(workerID, false, nil)
	}
	return notifyHijackOff, nil
}

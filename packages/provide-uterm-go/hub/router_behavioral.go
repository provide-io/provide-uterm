//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "context"

// keystrokeRingMax bounds the per-browser keystroke timing ring (Python
// deque(maxlen=50)).
const keystrokeRingMax = 50

// RecordKeystroke records the timing of a keystroke from source. Port of
// router_behavioral.record_keystroke.
func (r *MessageRouter) RecordKeystroke(source BrowserConn) {
	r.keystrokeMu.Lock()
	defer r.keystrokeMu.Unlock()
	ring := append(r.keystrokes[source], r.hub.clock.Monotonic())
	if len(ring) > keystrokeRingMax {
		ring = ring[len(ring)-keystrokeRingMax:]
	}
	r.keystrokes[source] = ring
}

// GetHeuristics returns behavioral metrics for source. Port of get_heuristics.
// With fewer than two samples it returns zero cps/jitter.
func (r *MessageRouter) GetHeuristics(source BrowserConn) map[string]float64 {
	r.keystrokeMu.Lock()
	ring := append([]float64(nil), r.keystrokes[source]...)
	r.keystrokeMu.Unlock()

	if len(ring) < 2 {
		return map[string]float64{"cps": 0.0, "jitter": 0.0}
	}
	duration := ring[len(ring)-1] - ring[0]
	cps := 0.0
	if duration > 0 {
		cps = float64(len(ring)-1) / duration
	}
	intervals := make([]float64, 0, len(ring)-1)
	for i := 1; i < len(ring); i++ {
		intervals = append(intervals, ring[i]-ring[i-1])
	}
	jitter := 0.0
	if len(intervals) > 1 {
		jitter = sampleVariance(intervals)
	}
	return map[string]float64{"cps": cps, "jitter": jitter}
}

// ForgetBrowser drops heuristic state for a disconnected browser. Port of
// forget_browser.
func (r *MessageRouter) ForgetBrowser(ws BrowserConn) {
	r.keystrokeMu.Lock()
	defer r.keystrokeMu.Unlock()
	delete(r.keystrokes, ws)
}

// AuditAllBrowsers iterates all active browsers and evaluates behavioral
// heuristics against the configured gate, closing connections the gate denies.
// Port of audit_all_browsers.
func (r *MessageRouter) AuditAllBrowsers(ctx context.Context) error {
	hub := r.hub
	type wb struct {
		workerID string
		ws       BrowserConn
	}
	hub.lock.Lock()
	var all []wb
	for _, wid := range hub.registry.Keys() {
		st := hub.registry.Get(wid)
		if st == nil {
			continue
		}
		for ws := range st.Browsers {
			all = append(all, wb{wid, ws})
		}
	}
	hub.lock.Unlock()

	for _, e := range all {
		hd := r.GetHeuristics(e.ws)
		heuristics := ConnectionHeuristics{CPS: hd["cps"], Jitter: hd["jitter"], Timestamp: hub.clock.Wall()}
		pc, err := hub.preparePolicyContext(ctx, e.ws, e.workerID, strp("behavioral_audit"))
		if err != nil {
			return err
		}
		decision, err := hub.behavioralAuditGate.AuditConnection(ctx, heuristics, pc, hub.behavioralThresh)
		if err != nil {
			return err
		}
		if decision.Action == "deny" {
			reason := "Behavioral anomaly"
			if decision.Reason != nil {
				reason = *decision.Reason
			}
			hub.logger.Warn("behavioral_audit_denied", "worker_id", e.workerID, "reason", reason)
			if closer, ok := e.ws.(BrowserCloser); ok {
				_ = closer.Close(ctx, 1008, reason) //nolint:errcheck // best-effort close
			}
		}
	}
	return nil
}

// sampleVariance returns the sample variance (ddof=1) of xs, matching Python's
// statistics.variance. Callers guarantee len(xs) >= 2.
func sampleVariance(xs []float64) float64 {
	n := float64(len(xs))
	mean := 0.0
	for _, x := range xs {
		mean += x
	}
	mean /= n
	sum := 0.0
	for _, x := range xs {
		d := x - mean
		sum += d * d
	}
	return sum / (n - 1)
}

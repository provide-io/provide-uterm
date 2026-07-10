//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// OutputCollector accumulates terminal output from a single session via the
// hub EventBus. Port of fanout/_collector.py.
//
// It subscribes to the worker's "term" and "snapshot" events and returns when:
//   - no new output has arrived for quiesceMS (adaptive quiesce), or
//   - the total elapsed time reaches maxMS (hard cap), or
//   - the worker disconnects (nil sentinel from the bus), or
//   - ctx is cancelled.
//
// "term" event deltas take priority; when only "snapshot" events arrive the
// last snapshot screen is returned so snapshot-only connectors still yield
// output. If bus is nil, Collect returns ("", 0) immediately.
//
// Deviation: like the Python collector, timing uses the real monotonic clock
// (Python reads time.monotonic directly), not an injected clock — the quiesce /
// hard-cap waits are inherently wall-time bound.
type OutputCollector struct{}

// Collect subscribes to the bus and accumulates output for workerID.
func (OutputCollector) Collect(
	ctx context.Context, bus *hub.EventBus, workerID string, quiesceMS, maxMS int,
) (string, int) {
	if bus == nil {
		return "", 0
	}
	sub, remove, err := bus.Watch(workerID, []string{"term", "snapshot"}, nil)
	if err != nil {
		return "", 0
	}
	defer remove()

	quiesce := time.Duration(quiesceMS) * time.Millisecond
	start := time.Now()
	deadline := start.Add(time.Duration(maxMS) * time.Millisecond)

	var termChunks strings.Builder
	hadTerm := false
	lastSnapshot := ""

loop:
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		wait := quiesce
		if remaining < wait {
			wait = remaining
		}
		timer := time.NewTimer(wait)
		select {
		case ev := <-sub.Queue:
			timer.Stop()
			if ev == nil {
				// Worker-disconnected sentinel.
				break loop
			}
			etype, _ := ev["type"].(string)
			data, _ := ev["data"].(map[string]any)
			if etype == "term" {
				if text, _ := data["data"].(string); text != "" {
					termChunks.WriteString(text)
					hadTerm = true
				}
			} else {
				// The subscription filter restricts to {term, snapshot}, so the
				// non-term branch is always the snapshot path.
				if screen, _ := data["screen"].(string); screen != "" {
					lastSnapshot = screen
				}
			}
		case <-timer.C:
			// Quiesced (no event within the window), or the hard cap was hit
			// when remaining < quiesce.
			break loop
		case <-ctx.Done():
			timer.Stop()
			break loop
		}
	}

	elapsedMS := int(time.Since(start) / time.Millisecond)
	if hadTerm {
		return termChunks.String(), elapsedMS
	}
	return lastSnapshot, elapsedMS
}

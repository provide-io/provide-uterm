//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"context"
	"strings"
	"sync"
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

// Capture is an output subscription prepared before worker input is sent.
// Close is idempotent so every dispatch exit path can release it safely.
type Capture struct {
	sub       *hub.Subscription
	remove    func()
	closeOnce sync.Once

	// DeadlineExceeded reports whether the last Collect ended on maxMS rather
	// than on quiet. The controller reports such a member as not ok: a
	// response cut off at the budget is not a complete one, and returning it
	// as ok makes truncation indistinguishable from a member that simply
	// finished quickly.
	DeadlineExceeded bool
}

// OpenCapture registers the worker's output subscription without starting its
// response timers. A nil bus preserves the historical no-output behavior.
func OpenCapture(bus *hub.EventBus, workerID string) (*Capture, error) {
	if bus == nil {
		return &Capture{}, nil
	}
	sub, remove, err := bus.Watch(workerID, []string{"term", "snapshot"}, nil)
	if err != nil {
		return nil, err
	}
	return &Capture{sub: sub, remove: remove}, nil
}

// Close removes this capture's subscription exactly once.
func (c *Capture) Close() {
	if c == nil {
		return
	}
	c.closeOnce.Do(func() {
		if c.remove != nil {
			c.remove()
		}
	})
}

// Collect consumes already-buffered and future output until quiescence, the
// hard collection cap, disconnect, or context cancellation.
func (c *Capture) Collect(ctx context.Context, quiesceMS, maxMS int) (string, int) {
	if c == nil || c.sub == nil {
		return "", 0
	}
	out, elapsedMS, cutShort := collectSubscription(ctx, c.sub, quiesceMS, maxMS)
	c.DeadlineExceeded = cutShort
	return out, elapsedMS
}

// Collect subscribes to the bus and accumulates output for workerID.
func (OutputCollector) Collect(
	ctx context.Context, bus *hub.EventBus, workerID string, quiesceMS, maxMS int,
) (string, int) {
	if bus == nil {
		return "", 0
	}
	capture, err := OpenCapture(bus, workerID)
	if err != nil {
		return "", 0
	}
	defer capture.Close()
	return capture.Collect(ctx, quiesceMS, maxMS)
}

func collectSubscription(
	ctx context.Context, sub *hub.Subscription, quiesceMS, maxMS int,
) (string, int, bool) {

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
			// Quiet, or the cap cut a quiesce window short. Either way the
			// member had stopped talking, so what was collected is what it
			// had to say -- not a truncation. A group whose quiesce is longer
			// than its cap always lands here, and reporting every such member
			// as cut off would make that configuration fail wholesale.
			break loop
		case <-ctx.Done():
			timer.Stop()
			break loop
		}
	}

	elapsedMS := int(time.Since(start) / time.Millisecond)

	// Truncated means we stopped with more still queued -- the member had not
	// finished talking. Deriving it from what is left, rather than from WHICH
	// exit fired, is what makes it reliable: the loop only reaches the
	// remaining <= 0 exit if an event happens to land in the final
	// microseconds, so keying on that lost the truncation whenever the
	// producer stalled near the end. It also keeps a group whose quiesce is
	// longer than its cap correct, since a member that answered and went
	// quiet leaves nothing pending.
	deadlineExceeded := sub.Pending() > 0

	if hadTerm {
		return termChunks.String(), elapsedMS, deadlineExceeded
	}
	return lastSnapshot, elapsedMS, deadlineExceeded
}

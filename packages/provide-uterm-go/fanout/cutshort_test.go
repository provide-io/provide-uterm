//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"errors"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// TestFanoutCutShortMemberIsFailedWithPartialOutput proves, in both modes, that
// a member still holding queued output when collection stopped is reported as
// failed but keeps the partial output on its row -- a truncated response must
// never read as a complete one.
func TestFanoutCutShortMemberIsFailedWithPartialOutput(t *testing.T) {
	for _, mode := range []string{"parallel", "sequential"} {
		t.Run(mode, func(t *testing.T) {
			bus := hub.NewEventBus(hub.EventBusOptions{})
			// w1 always reports more queued output; w2 drains cleanly.
			bus.PendingOverride = func(workerID string) (int, bool) {
				return 3, workerID == "w1"
			}
			fh := newFakeHub(bus, "w1", "w2")
			fh.onSend = func(workerID string) {
				bus.Enqueue(workerID, map[string]any{"type": "term", "data": map[string]any{"data": "out-" + workerID}})
			}
			ctrl := newCtrl(fh)
			_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2"}, func(g *Group) {
				g.Mode = mode
			}), "admin")

			r := mustSend(t, ctrl, "g1", "cmd\n", "admin", 5, 2000)

			if len(r.Results) != 2 {
				t.Fatalf("results = %+v", r.Results)
			}
			w1, w2 := r.Results[0], r.Results[1]
			if w1.WorkerID != "w1" || w1.OK || derefStr(w1.OutputDelta) != "out-w1" {
				t.Fatalf("w1 = %+v (delta %q), want failed with partial output", w1, derefStr(w1.OutputDelta))
			}
			if w2.WorkerID != "w2" || !w2.OK || derefStr(w2.OutputDelta) != "out-w2" {
				t.Fatalf("w2 = %+v, want ok", w2)
			}
			if len(r.FailedSessions) != 1 || r.FailedSessions[0] != "w1" {
				t.Fatalf("failed = %v, want [w1]", r.FailedSessions)
			}
		})
	}
}

// TestFanoutSequentialCaptureOpenFailureSkipsMember proves the sequential path
// never sends input to a member whose output capture could not be opened, and
// still goes on to the next member.
func TestFanoutSequentialCaptureOpenFailureSkipsMember(t *testing.T) {
	bus := hub.NewEventBus(hub.EventBusOptions{})
	fh := newFakeHub(bus, "w1", "w2")
	ctrl := newCtrl(fh)
	ctrl.openCapture = func(b *hub.EventBus, workerID string) (*Capture, error) {
		if workerID == "w1" {
			return nil, errors.New("no capture")
		}
		return OpenCapture(b, workerID)
	}
	_, _ = ctrl.CreateGroup(newGroup(t, []string{"w1", "w2"}, func(g *Group) {
		g.Mode = "sequential"
	}), "admin")

	r := mustSend(t, ctrl, "g1", "cmd\n", "admin", 5, 50)

	for _, call := range fh.sendCalls() {
		if call.WorkerID == "w1" {
			t.Fatal("member without a capture received input")
		}
	}
	if len(fh.sendCalls()) != 1 {
		t.Fatalf("sends = %+v, want exactly one (w2)", fh.sendCalls())
	}
	if r.Results[0].OK || r.Results[0].OutputDelta != nil || !r.Results[1].OK {
		t.Fatalf("results = %+v", r.Results)
	}
	if len(r.FailedSessions) != 1 || r.FailedSessions[0] != "w1" {
		t.Fatalf("failed = %v, want [w1]", r.FailedSessions)
	}
}

// TestCaptureCloseNilIsNoop proves Close on a nil capture is safe, so callers
// can defer it before checking OpenCapture's error.
func TestCaptureCloseNilIsNoop(t *testing.T) {
	var c *Capture
	c.Close()
}

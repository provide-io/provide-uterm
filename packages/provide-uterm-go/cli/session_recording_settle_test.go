//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"math"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// A recording can keep the screen of a session nobody is reading. A connector
// that emits raw output (a PAM capture session) records a screen only when one
// is sent, and one is sent only when a viewer asks: a session nobody watched
// held its opening blank screen and nothing else, and replay showed that.
// recording.screen_interval_s records the screen as it changes instead.
// Mirrors the reference's tests/server/test_runtime_settled_screens.py.

const (
	settle   = time.Duration(defaults.RecordedScreenSettleS * float64(time.Second))
	interval = time.Second
)

// settleClock is a clock a test moves by hand.
type settleClock struct{ t time.Time }

func (c *settleClock) now() time.Time { return c.t }

func (c *settleClock) advance(d time.Duration) { c.t = c.t.Add(d) }

// clockedRecorderAt is a recording recorder asking for a screen at most every
// screenInterval, open, on a hand-moved clock; recorded lists the screens its
// "read" entries hold.
func clockedRecorderAt(t *testing.T, screenInterval time.Duration) (*sessionRecorder, *settleClock, func() []string) {
	t.Helper()
	cfg := testRecordingConfig()
	cfg.ScreenIntervalS = screenInterval.Seconds()
	rec, store := newTestRecorder(t, cfg)
	clock := &settleClock{t: time.Unix(100, 0)}
	rec.now = clock.now
	rec.AttemptStarted()
	recorded := func() []string {
		t.Helper()
		must(t, rec.flush())
		var screens []string
		for _, e := range entries(t, store, "read") {
			screens = append(screens, e["data"].(map[string]any)["screen"].(string))
		}
		return screens
	}
	return rec, clock, recorded
}

func clockedRecorder(t *testing.T) (*sessionRecorder, *settleClock, func() []string) {
	t.Helper()
	return clockedRecorderAt(t, interval)
}

// screens hands out the given screens in turn, as the session's emulator
// would, counting how many were taken.
type screens struct {
	next  []string
	taken int
}

func (s *screens) take() map[string]any {
	s.taken++
	frame := snapshotFrame(s.next[0])
	s.next = s.next[1:]
	return frame
}

func output(t *testing.T, rec *sessionRecorder) {
	t.Helper()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "x"})
}

func wantRecorded(t *testing.T, recorded func() []string, want ...string) {
	t.Helper()
	if got := recorded(); !slices.Equal(got, want) {
		t.Fatalf("recorded %q, want %q", got, want)
	}
}

func TestABurstOfOutputIsRecordedOnceItSettles(t *testing.T) {
	rec, clock, recorded := clockedRecorder(t)
	src := &screens{next: []string{"painted"}}
	output(t, rec)
	must(t, rec.RecordSettledScreen(src.take))
	clock.advance(settle / 2)
	output(t, rec)
	must(t, rec.RecordSettledScreen(src.take))
	wantRecorded(t, recorded)
	clock.advance(settle)
	must(t, rec.RecordSettledScreen(src.take))
	must(t, rec.RecordSettledScreen(src.take))
	wantRecorded(t, recorded, "painted")
}

func TestOutputThatNeverStopsIsRecordedOnceAnInterval(t *testing.T) {
	rec, clock, recorded := clockedRecorder(t)
	src := &screens{next: []string{"first", "second"}}
	for range 10 {
		output(t, rec)
		must(t, rec.RecordSettledScreen(src.take))
		clock.advance(settle)
	}
	// A chunk every 0.25 s for 2.5 s: a screen 1 s in, and another 1 s after
	// the first chunk that screen did not hold.
	wantRecorded(t, recorded, "first", "second")
}

func TestScreensAreNeverCloserThanTheInterval(t *testing.T) {
	rec, clock, recorded := clockedRecorder(t)
	src := &screens{next: []string{"one", "two"}}
	output(t, rec)
	clock.advance(settle)
	must(t, rec.RecordSettledScreen(src.take))
	// A short burst that settles well inside the interval waits for it.
	output(t, rec)
	clock.advance(settle)
	must(t, rec.RecordSettledScreen(src.take))
	wantRecorded(t, recorded, "one")
	clock.advance(interval)
	must(t, rec.RecordSettledScreen(src.take))
	wantRecorded(t, recorded, "one", "two")
}

func TestAScreenLikeTheLastOneRecordedIsNotRecordedAgain(t *testing.T) {
	rec, clock, recorded := clockedRecorder(t)
	src := &screens{next: []string{"same", "same"}}
	for range 2 {
		output(t, rec)
		clock.advance(interval)
		must(t, rec.RecordSettledScreen(src.take))
	}
	wantRecorded(t, recorded, "same")
	if src.taken != 2 {
		t.Fatalf("the screen was taken %d times, want 2", src.taken)
	}
}

func TestAScreenAViewerReadCountsAsRecorded(t *testing.T) {
	rec, clock, recorded := clockedRecorder(t)
	src := &screens{next: []string{"unwanted"}}
	output(t, rec)
	sendFrame(t, rec, snapshotFrame("read by a viewer"))
	clock.advance(interval * 2)
	must(t, rec.RecordSettledScreen(src.take))
	wantRecorded(t, recorded, "read by a viewer")
	if src.taken != 0 {
		t.Fatalf("a screen was built %d times though nothing was owed", src.taken)
	}
}

// Off by default; and asked for but not recorded is off too.
func TestNothingIsTakenUnlessAskedForAndRecorded(t *testing.T) {
	for _, tc := range []struct {
		name     string
		interval time.Duration
		recorded bool
	}{
		{"off by default", 0, true},
		{"not recorded", interval, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := testRecordingConfig()
			cfg.ScreenIntervalS = tc.interval.Seconds()
			rec, store := newTestRecorder(t, cfg)
			rec.setEnabled(tc.recorded)
			clock := &settleClock{t: time.Unix(100, 0)}
			rec.now = clock.now
			rec.AttemptStarted()
			src := &screens{next: []string{"unwanted"}}
			output(t, rec)
			clock.advance(interval * 2)
			must(t, rec.RecordSettledScreen(src.take))
			if src.taken != 0 {
				t.Fatal("a screen was built")
			}
			if rec.recordsScreens() {
				t.Fatal("the bridge would be given a settle loop")
			}
			rec.AttemptEnded(nil)
			if got := entries(t, store, "read"); len(got) != 0 {
				t.Fatalf("recorded %v", got)
			}
		})
	}
}

func TestTheScreenIntervalIsOffByDefault(t *testing.T) {
	if got := serverconfig.DefaultServerConfig().Recording.ScreenIntervalS; got != 0 {
		t.Fatalf("screen_interval_s defaults to %v, want 0", got)
	}
}

// Output sent while the screen is being built -- the emulator runs ahead of
// the wire, so its term frame can be noted after the screen was read -- stays
// owed a screen: once it settles, a later tick records again. Otherwise the
// last chunk of a burst could land there and the final screen go unrecorded.
func TestOutputSentWhileTheScreenIsBuiltIsStillOwedAScreen(t *testing.T) {
	for _, tc := range []struct {
		name    string
		screens []string
	}{
		{"a new screen", []string{"mid-stream", "final"}},
		{"the screen already recorded", []string{"same", "same", "final"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec, clock, recorded := clockedRecorder(t)
			src := &screens{next: tc.screens}
			if len(tc.screens) == 3 {
				// Record "same" first, so the racing tick takes the
				// identical-screen branch.
				output(t, rec)
				clock.advance(interval)
				must(t, rec.RecordSettledScreen(src.take))
			}
			output(t, rec)
			clock.advance(interval)
			must(t, rec.RecordSettledScreen(func() map[string]any {
				frame := src.take()
				output(t, rec) // the last chunk, written while the screen was read
				return frame
			}))
			clock.advance(interval)
			must(t, rec.RecordSettledScreen(src.take))
			want := []string{"mid-stream", "final"}
			if len(tc.screens) == 3 {
				want = []string{"same", "final"}
			}
			wantRecorded(t, recorded, want...)
		})
	}
}

// No accepted interval overflows a Duration into a negative one (which would
// turn recording off): a huge one is held at the largest Duration, and the
// first screen is still recorded.
func TestAHugeScreenIntervalDoesNotOverflow(t *testing.T) {
	for _, v := range []float64{1e10, 1e300, maxScreenIntervalS, math.Inf(1)} {
		cfg := testRecordingConfig()
		cfg.ScreenIntervalS = v
		rec, _ := newTestRecorder(t, cfg)
		if got := rec.screenInterval(); got <= 0 {
			t.Fatalf("screen_interval_s = %v became %v", v, got)
		}
	}
	for _, v := range []float64{0, -1, math.NaN()} {
		cfg := testRecordingConfig()
		cfg.ScreenIntervalS = v
		rec, _ := newTestRecorder(t, cfg)
		if got := rec.screenInterval(); got != 0 {
			t.Fatalf("screen_interval_s = %v became %v, want off", v, got)
		}
	}
	rec, clock, recorded := clockedRecorderAt(t, time.Duration(math.MaxInt64))
	rec.cfg.ScreenIntervalS = 1e300
	output(t, rec)
	clock.advance(settle)
	must(t, rec.RecordSettledScreen((&screens{next: []string{"only"}}).take))
	wantRecorded(t, recorded, "only")
}

// A screen recorded unasked is logged as a "read" and nothing else: it was
// never sent, so it has no wire entry, whatever the control-channel mode.
func TestASettledScreenHasNoWireEntry(t *testing.T) {
	cfg := testRecordingConfig()
	cfg.ControlChannelMode = "wire"
	cfg.ScreenIntervalS = interval.Seconds()
	rec, store := newTestRecorder(t, cfg)
	clock := &settleClock{t: time.Unix(100, 0)}
	rec.now = clock.now
	rec.AttemptStarted()
	output(t, rec)
	clock.advance(settle)
	must(t, rec.RecordSettledScreen((&screens{next: []string{"painted"}}).take))
	rec.AttemptEnded(nil)
	// The one wire entry is the term chunk's: the screen added none.
	want := []string{"log_start", "wire_send", "read", "log_stop"}
	if got := eventNames(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("events = %v, want %v", got, want)
	}
}

// A recording whose write failed reports it at the next settle tick, before
// building anything, as every other observed point does.
func TestASettledScreenReportsARefusedRead(t *testing.T) {
	rec, clock, _ := clockedRecorder(t)
	wrapLog(t, rec, func(l recordingLog) recordingLog { return refusedRead{l} })
	output(t, rec)
	clock.advance(settle)
	if err := rec.RecordSettledScreen((&screens{next: []string{"painted"}}).take); err != errLogWrite {
		t.Fatalf("RecordSettledScreen = %v, want the refused write", err)
	}
}

type refusedRead struct{ recordingLog }

func (refusedRead) LogScreenFrame(map[string]any, []byte) error { return errLogWrite }

// End to end on the worker side: a pty_capture session nobody reads, recording
// its screen every 0.25 s and bridged to a hub that never asks for a snapshot,
// still has the screen its output painted recorded -- and the hub is never
// sent a snapshot for it.
func TestAnUnreadCaptureSessionRecordsWhatItPainted(t *testing.T) {
	sock := shortCaptureDir(t) + "/cap.sock"
	conn, err := connectors.Build("cap", "Cap", "pty_capture", map[string]any{
		"socket_path": sock, "cols": 80, "rows": 24,
	})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = conn.Stop(context.Background()) }()

	cfg := testRecordingConfig()
	cfg.ScreenIntervalS = settle.Seconds()
	rec, store := newTestRecorder(t, cfg)
	sink, hubURL := newWorkerSink(t)
	br := bridge.New(bridge.Config{
		Worker: &sessionWorker{conn: conn}, WorkerID: "cap", ManagerURL: hubURL, Encoding: "latin-1",
		Observer: rec, RecordScreens: rec.recordsScreens(),
	})
	br.Start(ctx)
	defer br.Stop()
	waitFor(t, "the worker socket", sink.helloed)

	writeCaptureFrame(t, sock, uptty.ChannelStdout, "hello from nobody's session")
	waitFor(t, "the painted screen recorded", func() bool {
		must(t, rec.flush())
		for _, e := range entries(t, store, "read") {
			if s, _ := e["data"].(map[string]any)["screen"].(string); strings.Contains(s, "hello from nobody's session") {
				return true
			}
		}
		return false
	})
	sink.mu.Lock()
	defer sink.mu.Unlock()
	for _, f := range sink.ctrl {
		if f["type"] == "snapshot" {
			t.Fatalf("the hub was sent a snapshot nobody asked for: %v", f)
		}
	}
}

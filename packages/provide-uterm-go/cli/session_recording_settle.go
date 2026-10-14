//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"math"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// The settled-screen half of sessionRecorder (recording.screen_interval_s):
// noting output no recorded screen holds yet, and recording the session's own
// screen once that output settles. It is kept apart from session_recording.go,
// which holds the rest of the recording lifecycle, so that neither file grows
// past the repository's line limit; the state these methods share is declared
// on sessionRecorder there.

// noteOutput is _note_output: terminal output was sent that no recorded
// screen holds yet. Noted whether or not a recording is open, as the
// reference notes it with or without a logger. Caller holds s.mu.
func (s *sessionRecorder) noteOutput() {
	now := s.now()
	s.lastOutputAt = now
	s.outputs++
	if s.unrecordedSince.IsZero() {
		s.unrecordedSince = now
	}
}

// screenInterval is recording.screen_interval_s: how often, at most, the
// session's own screen is recorded as it changes; 0 or less is off, and only
// the screens sent to a viewer are recorded.
//
// The config refuses anything but 0 or a finite number of seconds >= the
// settle period, but the conversion is guarded all the same: a value past
// what a Duration holds (about 292 years) would overflow to a negative one,
// silently turning recording off, so it is held at the largest Duration
// instead, which records the first screen and, like the reference's huge
// interval, never another.
func (s *sessionRecorder) screenInterval() time.Duration {
	v := s.cfg.ScreenIntervalS
	if !(v > 0) { //nolint:staticcheck // QF1001: `!(v > 0)` is deliberate -- it treats NaN as off
		return 0
	}
	if v >= maxScreenIntervalS {
		return time.Duration(math.MaxInt64)
	}
	return time.Duration(v * float64(time.Second))
}

// maxScreenIntervalS is the largest whole number of seconds a Duration holds;
// screenInterval holds anything at or past it at the largest Duration.
const maxScreenIntervalS = float64(math.MaxInt64 / int64(time.Second))

// recordsScreens reports whether the session records its own screen as it
// changes (recording.screen_interval_s > 0, and the session is recorded); the
// registry gives the bridge a settle loop only then (bridge.Config
// RecordScreens), so a session that does not runs no ticker at all.
func (s *sessionRecorder) recordsScreens() bool {
	return s.screenInterval() > 0 && s.isEnabled()
}

// settledScreenDue is _record_settled_screen's test: screens are asked for
// (screen_interval_s > 0), a recording is open, output has gone unrecorded,
// the last screen recorded is at least an interval old, and either the
// output has paused for the settle period or the oldest of it has waited an
// interval. Taken after a pause rather than per chunk because a repaint
// arrives as many chunks, and a screen for each would be a recording of
// halves; taken anyway once the oldest unrecorded output is an interval old,
// so output that never pauses is recorded once an interval. Caller holds
// s.mu.
func (s *sessionRecorder) settledScreenDue() bool {
	interval := s.screenInterval()
	if interval <= 0 || s.rec == nil || s.unrecordedSince.IsZero() {
		return false
	}
	now := s.now()
	if s.screenRecorded && now.Sub(s.lastScreenRecordedAt) < interval {
		return false
	}
	settle := time.Duration(defaults.RecordedScreenSettleS * float64(time.Second))
	quiet := now.Sub(s.lastOutputAt) >= settle
	return quiet || now.Sub(s.unrecordedSince) >= interval
}

// RecordSettledScreen is _record_settled_screen: when the open recording is
// due a screen (settledScreenDue), the session's current one is logged as a
// "read" through logSnapshot, exactly as a snapshot a viewer was sent is,
// but with no wire entry, since it is never sent; unless it is the same
// screen as the last one recorded, which settles the output and records
// nothing. A connector that streams
// raw output (a PAM capture) sends a screen only when a viewer asks for one,
// and only a sent screen was recorded, so the recording of a session nobody
// read held its opening screen and nothing after it.
//
// Nothing is built while the session is unrecorded or nothing is owed. The
// screen is built outside mu, since it reads the session's emulator, which
// the recorder must never hold its lock across. Two things can happen in
// that window that cannot in the reference, whose _note_output runs only on
// the coroutine that awaits get_snapshot:
//   - A viewer's snapshot is logged, recording the screen already, so the
//     check is repeated before logging (the reference would log it twice).
//   - Output is sent and noted (FrameSent runs on the send goroutine). The
//     emulator runs ahead of the wire, so that output may or may not be on
//     the screen just built; it stays owed a screen, from the time it was
//     noted, rather than being cleared with the rest. Otherwise the last
//     chunk of a burst could land there, and with output then stopped, the
//     session's final screen would go unrecorded -- the very failure this
//     exists to fix.
func (s *sessionRecorder) RecordSettledScreen(screenFn func() map[string]any) error {
	s.mu.Lock()
	if err := s.failed(); err != nil {
		s.mu.Unlock()
		return err
	}
	// Past its quota the recording drops every write: a screen built for it
	// would be rendered only to be thrown away.
	due := s.settledScreenDue() && !s.rec.QuotaReached()
	outputs := s.outputs
	s.mu.Unlock()
	if !due {
		return nil
	}
	frame := screenFn()
	if frame == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.rec == nil || s.unrecordedSince.IsZero() {
		return nil
	}
	if err := s.failed(); err != nil {
		return err
	}
	// Output noted while the screen was built stays owed, whichever way this
	// goes (see above).
	defer s.keepOutputSince(outputs)
	if raw, _ := frame["screen"].(string); s.screenRecorded && raw == s.lastScreenRecorded {
		s.unrecordedSince = time.Time{}
		return nil
	}
	return s.logScreen(frame, false)
}

// keepOutputSince leaves output noted since the outputs count read owed a
// screen: if any came, unrecordedSince is the time of the latest rather than
// unset. Caller holds s.mu.
func (s *sessionRecorder) keepOutputSince(outputs uint64) {
	if s.outputs != outputs {
		s.unrecordedSince = s.lastOutputAt
	}
}

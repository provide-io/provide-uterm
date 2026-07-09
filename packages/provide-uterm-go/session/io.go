//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package session

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Defaults mirroring provide.uterm.io module constants.
const (
	DefaultPromptTimeout        = 10 * time.Second
	DefaultPromptReadInterval   = 250 * time.Millisecond
	DefaultPromptRequireIdle    = true
	DefaultPromptIdleGraceRatio = 0.8
	DefaultInputType            = "multi_key"
	DefaultWaitAfter            = 200 * time.Millisecond
)

// ErrNoSession is returned when a waiter/sender has no session configured.
var ErrNoSession = errors.New("session is nil")

// ErrDisconnected is returned when the session reports it is not connected.
var ErrDisconnected = errors.New("session disconnected")

// PromptResult is the accepted-prompt result of WaitForPrompt.
type PromptResult struct {
	Screen    string
	PromptID  string
	InputType string
	KVData    map[string]any
	IsIdle    bool
}

// PromptCandidate is the full candidate passed to the observer callbacks:
// the detection plus the screen context it was seen on.
type PromptCandidate struct {
	Detection  PromptDetection
	Screen     string
	ScreenHash string
	CapturedAt float64
}

// TimeoutError reports that no matching prompt was detected in time.
type TimeoutError struct {
	Timeout time.Duration
}

func (e *TimeoutError) Error() string {
	return fmt.Sprintf("no prompt detected within %dms", e.Timeout.Milliseconds())
}

// WaitOptions configure PromptWaiter.WaitForPrompt. Zero values select the
// package defaults.
type WaitOptions struct {
	// ExpectedPromptID, when non-empty, only accepts prompts whose PromptID
	// contains it.
	ExpectedPromptID string
	// Timeout is the maximum wait; zero selects DefaultPromptTimeout.
	Timeout time.Duration
	// ReadInterval is the polling backstop; zero selects
	// DefaultPromptReadInterval.
	ReadInterval time.Duration
	// OnPromptDetected, when set, filters candidates: return false to reject.
	OnPromptDetected func(PromptCandidate) bool
	// OnPromptSeen fires for every candidate prompt.
	OnPromptSeen func(PromptCandidate)
	// OnPromptRejected fires when a candidate is rejected, with a reason of
	// "not_idle", "expected_mismatch", or "callback_reject".
	OnPromptRejected func(PromptCandidate, string)
	// RequireIdle waits for the screen to stabilize before returning; nil
	// selects DefaultPromptRequireIdle.
	RequireIdle *bool
	// IdleGraceRatio accepts a non-idle prompt after this fraction of the
	// timeout has elapsed; zero selects DefaultPromptIdleGraceRatio.
	IdleGraceRatio float64
}

// PromptWaiter waits for a prompt to appear in the session snapshot. Port of
// provide.uterm.io.PromptWaiter.
type PromptWaiter struct {
	// Session is the underlying terminal session.
	Session Session
	// OnScreenUpdate, when set, is invoked with the raw screen text on each
	// poll.
	OnScreenUpdate func(string)
	// now/monotonic clock, overridable in tests.
	now func() time.Time
}

// NewPromptWaiter creates a PromptWaiter over session.
func NewPromptWaiter(session Session, onScreenUpdate func(string)) *PromptWaiter {
	return &PromptWaiter{Session: session, OnScreenUpdate: onScreenUpdate, now: time.Now}
}

func (w *PromptWaiter) assertConnected() error {
	if w.Session == nil {
		return ErrNoSession
	}
	if !sessionConnected(w.Session) {
		return ErrDisconnected
	}
	return nil
}

// waitIfNotIdle reports whether this candidate should be skipped because the
// screen is not yet idle (waiting out the remaining idle time first).
func (w *PromptWaiter) waitIfNotIdle(
	ctx context.Context,
	candidate PromptCandidate,
	elapsed, timeout time.Duration,
	opts *WaitOptions,
	readInterval time.Duration,
	requireIdle bool,
	idleGraceRatio float64,
) (bool, error) {
	grace := time.Duration(float64(timeout) * idleGraceRatio)
	if !requireIdle || candidate.Detection.IsIdle || elapsed >= grace {
		return false, nil
	}
	if opts.OnPromptRejected != nil {
		opts.OnPromptRejected(candidate, "not_idle")
	}
	remainingIdle := readInterval
	if reporter, ok := w.Session.(IdleReporter); ok {
		remainingIdle = time.Duration(reporter.SecondsUntilIdle() * float64(time.Second))
	}
	wait := max(time.Millisecond, min(remainingIdle, timeout-elapsed))
	if _, err := w.Session.WaitForUpdate(ctx, wait); err != nil {
		return false, err
	}
	return true, nil
}

// checkPromptFilters reports whether the candidate was rejected by the
// expected-id or callback filters (the caller continues polling).
func (w *PromptWaiter) checkPromptFilters(
	ctx context.Context,
	candidate PromptCandidate,
	opts *WaitOptions,
	readInterval time.Duration,
) (bool, error) {
	if opts.ExpectedPromptID != "" && !strings.Contains(candidate.Detection.PromptID, opts.ExpectedPromptID) {
		if opts.OnPromptRejected != nil {
			opts.OnPromptRejected(candidate, "expected_mismatch")
		}
		if _, err := w.Session.WaitForUpdate(ctx, readInterval); err != nil {
			return false, err
		}
		return true, nil
	}
	if opts.OnPromptDetected != nil && !opts.OnPromptDetected(candidate) {
		if opts.OnPromptRejected != nil {
			opts.OnPromptRejected(candidate, "callback_reject")
		}
		if _, err := w.Session.WaitForUpdate(ctx, readInterval); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

// WaitForPrompt polls the session until a matching prompt is detected,
// returning a TimeoutError when none appears within the timeout.
func (w *PromptWaiter) WaitForPrompt(ctx context.Context, opts WaitOptions) (PromptResult, error) {
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = DefaultPromptTimeout
	}
	readInterval := opts.ReadInterval
	if readInterval == 0 {
		readInterval = DefaultPromptReadInterval
	}
	requireIdle := DefaultPromptRequireIdle
	if opts.RequireIdle != nil {
		requireIdle = *opts.RequireIdle
	}
	idleGraceRatio := opts.IdleGraceRatio
	if idleGraceRatio == 0 {
		idleGraceRatio = DefaultPromptIdleGraceRatio
	}

	start := w.now()
	for w.now().Sub(start) < timeout {
		if err := w.assertConnected(); err != nil {
			return PromptResult{}, err
		}
		snap := w.Session.Snapshot()
		if w.OnScreenUpdate != nil {
			w.OnScreenUpdate(snap.Screen)
		}

		if snap.PromptDetected != nil {
			detection := *snap.PromptDetected
			candidate := PromptCandidate{
				Detection:  detection,
				Screen:     snap.Screen,
				ScreenHash: snap.ScreenHash,
				CapturedAt: snap.CapturedAt,
			}
			if opts.OnPromptSeen != nil {
				opts.OnPromptSeen(candidate)
			}

			elapsed := w.now().Sub(start)
			skip, err := w.waitIfNotIdle(ctx, candidate, elapsed, timeout, &opts, readInterval, requireIdle, idleGraceRatio)
			if err != nil {
				return PromptResult{}, err
			}
			if skip {
				continue
			}

			rejected, err := w.checkPromptFilters(ctx, candidate, &opts, readInterval)
			if err != nil {
				return PromptResult{}, err
			}
			if rejected {
				continue
			}

			return PromptResult{
				Screen:    snap.Screen,
				PromptID:  detection.PromptID,
				InputType: detection.InputType,
				KVData:    detection.KVData,
				IsIdle:    detection.IsIdle,
			}, nil
		}

		remaining := max(0, timeout-w.now().Sub(start))
		if _, err := w.Session.WaitForUpdate(ctx, min(readInterval, remaining)); err != nil {
			return PromptResult{}, err
		}
	}
	return PromptResult{}, &TimeoutError{Timeout: timeout}
}

// InputSender sends keystrokes to a session respecting input-type semantics.
// Port of provide.uterm.io.InputSender.
type InputSender struct {
	// Session is the underlying terminal session.
	Session Session
	// sleep is overridable in tests.
	sleep func(context.Context, time.Duration) error
}

// NewInputSender creates an InputSender over session.
func NewInputSender(session Session) *InputSender {
	return &InputSender{Session: session, sleep: sleepCtx}
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// SendInput sends input respecting the prompt type:
//
//   - "single_key": sends keys as-is (no newline)
//   - "any_key": sends a single space
//   - "multi_key" and anything else: appends \r
//
// inputType "" selects DefaultInputType; waitAfter < 0 selects
// DefaultWaitAfter, 0 skips the post-send sleep.
func (s *InputSender) SendInput(ctx context.Context, keys, inputType string, waitAfter time.Duration) error {
	if s.Session == nil {
		return ErrNoSession
	}
	if !sessionConnected(s.Session) {
		return ErrDisconnected
	}

	if inputType == "" {
		inputType = DefaultInputType
	}
	if waitAfter < 0 {
		waitAfter = DefaultWaitAfter
	}

	var err error
	switch inputType {
	case "single_key":
		err = s.Session.Send(ctx, keys)
	case "any_key":
		err = s.Session.Send(ctx, " ")
	default:
		err = s.Session.Send(ctx, keys+"\r")
	}
	if err != nil {
		return err
	}

	if waitAfter > 0 {
		return s.sleep(ctx, waitAfter)
	}
	return nil
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package session

import (
	"context"
	"errors"
	"testing"
	"time"
)

// fakeSession is a scriptable Session + ExpectSession.
type fakeSession struct {
	snapshots []Snapshot // consumed one per Snapshot() call; last repeats
	snapIdx   int
	sent      []string
	sendErr   error
	waitErr   error
	waits     []time.Duration
	changed   bool // WaitForScreenChange result
	seq       int
	connected bool
	idleSecs  float64
}

func (f *fakeSession) Snapshot() Snapshot {
	if f.snapIdx < len(f.snapshots)-1 {
		s := f.snapshots[f.snapIdx]
		f.snapIdx++
		return s
	}
	if len(f.snapshots) == 0 {
		return Snapshot{}
	}
	return f.snapshots[len(f.snapshots)-1]
}

func (f *fakeSession) Send(_ context.Context, data string) error {
	f.sent = append(f.sent, data)
	return f.sendErr
}

func (f *fakeSession) WaitForUpdate(_ context.Context, timeout time.Duration) (bool, error) {
	f.waits = append(f.waits, timeout)
	return true, f.waitErr
}

func (f *fakeSession) ScreenChangeSeq() int { f.seq++; return f.seq }

func (f *fakeSession) WaitForScreenChange(_ context.Context, timeout time.Duration, _ int) (bool, error) {
	f.waits = append(f.waits, timeout)
	return f.changed, f.waitErr
}

func (f *fakeSession) IsConnected() bool { return f.connected }

// idleSession adds SecondsUntilIdle.
type idleSession struct {
	fakeSession
}

func (s *idleSession) SecondsUntilIdle() float64 { return s.idleSecs }

func detectedSnap(promptID string, isIdle bool) Snapshot {
	return Snapshot{
		Screen:     "screen with " + promptID,
		ScreenHash: "hash",
		CapturedAt: 123.0,
		PromptDetected: &PromptDetection{
			PromptID:  promptID,
			InputType: "multi_key",
			IsIdle:    isIdle,
			KVData:    map[string]any{"credits": "42"},
		},
	}
}

func TestWaitForPromptAcceptsIdlePrompt(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{detectedSnap("command_prompt", true)}}
	w := NewPromptWaiter(fs, nil)
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if got.PromptID != "command_prompt" || !got.IsIdle || got.InputType != "multi_key" || got.KVData["credits"] != "42" {
		t.Fatalf("got %+v", got)
	}
}

func TestWaitForPromptExpectedIDFilter(t *testing.T) {
	var rejections []string
	fs := &fakeSession{connected: true, snapshots: []Snapshot{
		detectedSnap("other_prompt", true),
		detectedSnap("command_prompt", true),
	}}
	w := NewPromptWaiter(fs, nil)
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{
		ExpectedPromptID: "command",
		OnPromptRejected: func(_ PromptCandidate, reason string) { rejections = append(rejections, reason) },
	})
	if err != nil || got.PromptID != "command_prompt" {
		t.Fatalf("got %+v err %v", got, err)
	}
	if len(rejections) != 1 || rejections[0] != "expected_mismatch" {
		t.Fatalf("rejections = %v", rejections)
	}
}

func TestWaitForPromptCallbackReject(t *testing.T) {
	seen := 0
	fs := &fakeSession{connected: true, snapshots: []Snapshot{
		detectedSnap("p1", true),
		detectedSnap("p2", true),
	}}
	w := NewPromptWaiter(fs, nil)
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{
		OnPromptSeen:     func(PromptCandidate) { seen++ },
		OnPromptDetected: func(c PromptCandidate) bool { return c.Detection.PromptID == "p2" },
	})
	if err != nil || got.PromptID != "p2" {
		t.Fatalf("got %+v err %v", got, err)
	}
	if seen != 2 {
		t.Fatalf("seen = %d", seen)
	}
}

func TestWaitForPromptNotIdleWaits(t *testing.T) {
	s := &idleSession{}
	s.connected = true
	s.idleSecs = 0.05
	s.snapshots = []Snapshot{
		detectedSnap("p", false),
		detectedSnap("p", true),
	}
	w := NewPromptWaiter(s, nil)
	var reasons []string
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{
		OnPromptRejected: func(_ PromptCandidate, r string) { reasons = append(reasons, r) },
	})
	if err != nil || !got.IsIdle {
		t.Fatalf("got %+v err %v", got, err)
	}
	if len(reasons) != 1 || reasons[0] != "not_idle" {
		t.Fatalf("reasons = %v", reasons)
	}
	// The idle wait consulted SecondsUntilIdle (50ms).
	if len(s.waits) == 0 || s.waits[0] != 50*time.Millisecond {
		t.Fatalf("waits = %v", s.waits)
	}
}

func TestWaitForPromptNonIdleAcceptedAfterGrace(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{detectedSnap("p", false)}}
	w := NewPromptWaiter(fs, nil)
	// Force elapsed past the grace window by shifting the fake clock.
	base := time.Now()
	calls := 0
	w.now = func() time.Time {
		calls++
		if calls == 1 {
			return base // start
		}
		return base.Add(900 * time.Millisecond) // everything after: past 0.8*1s grace
	}
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{Timeout: time.Second})
	if err != nil || got.PromptID != "p" || got.IsIdle {
		t.Fatalf("got %+v err %v", got, err)
	}
}

func TestWaitForPromptRequireIdleFalse(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{detectedSnap("p", false)}}
	w := NewPromptWaiter(fs, nil)
	requireIdle := false
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{RequireIdle: &requireIdle})
	if err != nil || got.PromptID != "p" {
		t.Fatalf("got %+v err %v", got, err)
	}
}

func TestWaitForPromptTimesOut(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{{Screen: "no prompt"}}}
	var screens []string
	w := NewPromptWaiter(fs, func(s string) { screens = append(screens, s) })
	base := time.Now()
	step := 0
	w.now = func() time.Time {
		step++
		return base.Add(time.Duration(step) * 40 * time.Millisecond)
	}
	_, err := w.WaitForPrompt(context.Background(), WaitOptions{Timeout: 100 * time.Millisecond})
	var te *TimeoutError
	if !errors.As(err, &te) {
		t.Fatalf("err = %v", err)
	}
	if te.Error() != "no prompt detected within 100ms" {
		t.Fatalf("msg = %q", te.Error())
	}
	if len(screens) == 0 || screens[0] != "no prompt" {
		t.Fatalf("screens = %v", screens)
	}
}

func TestWaitForPromptConnectionErrors(t *testing.T) {
	w := NewPromptWaiter(nil, nil)
	if _, err := w.WaitForPrompt(context.Background(), WaitOptions{}); !errors.Is(err, ErrNoSession) {
		t.Fatalf("err = %v", err)
	}
	fs := &fakeSession{connected: false}
	w = NewPromptWaiter(fs, nil)
	if _, err := w.WaitForPrompt(context.Background(), WaitOptions{}); !errors.Is(err, ErrDisconnected) {
		t.Fatalf("err = %v", err)
	}
}

func TestWaitForPromptPropagatesWaitErrors(t *testing.T) {
	boom := errors.New("boom")
	// Error in the no-prompt poll path.
	fs := &fakeSession{connected: true, waitErr: boom, snapshots: []Snapshot{{Screen: "x"}}}
	w := NewPromptWaiter(fs, nil)
	if _, err := w.WaitForPrompt(context.Background(), WaitOptions{}); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	// Error in the not-idle wait path.
	fs2 := &fakeSession{connected: true, waitErr: boom, snapshots: []Snapshot{detectedSnap("p", false)}}
	w2 := NewPromptWaiter(fs2, nil)
	if _, err := w2.WaitForPrompt(context.Background(), WaitOptions{}); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	// Error in the expected-mismatch wait path.
	fs3 := &fakeSession{connected: true, waitErr: boom, snapshots: []Snapshot{detectedSnap("p", true)}}
	w3 := NewPromptWaiter(fs3, nil)
	if _, err := w3.WaitForPrompt(context.Background(), WaitOptions{ExpectedPromptID: "zz"}); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	// Error in the callback-reject wait path.
	fs4 := &fakeSession{connected: true, waitErr: boom, snapshots: []Snapshot{detectedSnap("p", true)}}
	w4 := NewPromptWaiter(fs4, nil)
	_, err := w4.WaitForPrompt(context.Background(), WaitOptions{
		OnPromptDetected: func(PromptCandidate) bool { return false },
	})
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
}

func TestWaitForPromptCallbackRejectFiresRejectedCallback(t *testing.T) {
	var reasons []string
	fs := &fakeSession{connected: true, snapshots: []Snapshot{
		detectedSnap("p1", true),
		detectedSnap("p2", true),
	}}
	w := NewPromptWaiter(fs, nil)
	got, err := w.WaitForPrompt(context.Background(), WaitOptions{
		OnPromptDetected: func(c PromptCandidate) bool { return c.Detection.PromptID == "p2" },
		OnPromptRejected: func(_ PromptCandidate, r string) { reasons = append(reasons, r) },
	})
	if err != nil || got.PromptID != "p2" {
		t.Fatalf("got %+v err %v", got, err)
	}
	if len(reasons) != 1 || reasons[0] != "callback_reject" {
		t.Fatalf("reasons = %v", reasons)
	}
}

func TestSendAndExpectDeadlineWithContinuousChanges(t *testing.T) {
	// The screen keeps changing but never matches: the loop must exit on the
	// deadline check at the top rather than the !changed branch.
	fs := &fakeSession{connected: true, changed: true, snapshots: []Snapshot{{Screen: "never matches"}}}
	got, err := SendAndExpect(context.Background(), fs, "x", ExpectOptions{ExpectText: "target", Timeout: 5 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	if !got.TimedOut || got.Matched {
		t.Fatalf("got %+v", got)
	}
}

func TestInputSenderModes(t *testing.T) {
	cases := []struct {
		inputType string
		keys      string
		want      string
	}{
		{"single_key", "q", "q"},
		{"any_key", "ignored", " "},
		{"multi_key", "look", "look\r"},
		{"", "buy", "buy\r"},
		{"unknown_type", "go", "go\r"},
	}
	for _, c := range cases {
		fs := &fakeSession{connected: true}
		s := NewInputSender(fs)
		s.sleep = func(context.Context, time.Duration) error { return nil }
		if err := s.SendInput(context.Background(), c.keys, c.inputType, -1); err != nil {
			t.Fatal(err)
		}
		if len(fs.sent) != 1 || fs.sent[0] != c.want {
			t.Fatalf("%s: sent %v want %q", c.inputType, fs.sent, c.want)
		}
	}
}

func TestInputSenderWaitAfterAndErrors(t *testing.T) {
	fs := &fakeSession{connected: true}
	s := NewInputSender(fs)
	slept := time.Duration(0)
	s.sleep = func(_ context.Context, d time.Duration) error { slept = d; return nil }
	if err := s.SendInput(context.Background(), "x", "single_key", 300*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if slept != 300*time.Millisecond {
		t.Fatalf("slept = %v", slept)
	}
	// waitAfter == 0 skips the sleep.
	slept = 0
	if err := s.SendInput(context.Background(), "x", "single_key", 0); err != nil {
		t.Fatal(err)
	}
	if slept != 0 {
		t.Fatalf("slept = %v", slept)
	}

	if err := NewInputSender(nil).SendInput(context.Background(), "x", "", 0); !errors.Is(err, ErrNoSession) {
		t.Fatalf("err = %v", err)
	}
	if err := NewInputSender(&fakeSession{}).SendInput(context.Background(), "x", "", 0); !errors.Is(err, ErrDisconnected) {
		t.Fatalf("err = %v", err)
	}
	boom := errors.New("send failed")
	fs2 := &fakeSession{connected: true, sendErr: boom}
	if err := NewInputSender(fs2).SendInput(context.Background(), "x", "", 0); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
}

func TestInputSenderRealSleepHonorsContext(t *testing.T) {
	fs := &fakeSession{connected: true}
	s := NewInputSender(fs)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err := s.SendInput(ctx, "x", "single_key", time.Hour)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
	// And the timer path completes normally.
	if err := s.SendInput(context.Background(), "x", "single_key", time.Millisecond); err != nil {
		t.Fatal(err)
	}
}

func TestSendAndExpectImmediateMatch(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{{Screen: "Command [TL=00:00:00]:"}}}
	got, err := SendAndExpect(context.Background(), fs, `look\r`, ExpectOptions{ExpectText: "Command"})
	if err != nil {
		t.Fatal(err)
	}
	if !got.Matched || got.MatchedText != "Command" || got.TimedOut {
		t.Fatalf("got %+v", got)
	}
	// Keys were sanitized: \r escape translated.
	if fs.sent[0] != "look\r" {
		t.Fatalf("sent %q", fs.sent[0])
	}
}

func TestSendAndExpectRegexAndPolling(t *testing.T) {
	fs := &fakeSession{connected: true, changed: true, snapshots: []Snapshot{
		{Screen: "loading"},
		{Screen: "sector 123 ready"},
	}}
	got, err := SendAndExpect(context.Background(), fs, "d", ExpectOptions{ExpectRegex: `sector \d+`})
	if err != nil {
		t.Fatal(err)
	}
	if !got.Matched || got.MatchedText != "sector 123" {
		t.Fatalf("got %+v", got)
	}
}

func TestSendAndExpectNoExpectationWaitsOnce(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{{Screen: "a"}, {Screen: "b"}}}
	got, err := SendAndExpect(context.Background(), fs, "x", ExpectOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if got.Matched || got.TimedOut || got.Screen != "b" {
		t.Fatalf("got %+v", got)
	}
	if len(fs.waits) != 1 {
		t.Fatalf("waits = %v", fs.waits)
	}
}

func TestSendAndExpectTimeout(t *testing.T) {
	fs := &fakeSession{connected: true, changed: false, snapshots: []Snapshot{{Screen: "nope"}}}
	got, err := SendAndExpect(context.Background(), fs, "x", ExpectOptions{ExpectText: "never", Timeout: 50 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	if got.Matched || !got.TimedOut || got.Screen != "nope" {
		t.Fatalf("got %+v", got)
	}
}

func TestSendAndExpectEmptyPayloadSkipsSend(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{{Screen: "prompt"}}}
	got, err := SendAndExpect(context.Background(), fs, "", ExpectOptions{ExpectText: "prompt"})
	if err != nil || !got.Matched {
		t.Fatalf("got %+v err %v", got, err)
	}
	if len(fs.sent) != 0 {
		t.Fatalf("sent = %v", fs.sent)
	}
}

func TestSendAndExpectNoSanitize(t *testing.T) {
	fs := &fakeSession{connected: true, snapshots: []Snapshot{{Screen: "ok"}}}
	noSanitize := false
	_, err := SendAndExpect(context.Background(), fs, "raw\x01bytes", ExpectOptions{ExpectText: "ok", Sanitize: &noSanitize})
	if err != nil {
		t.Fatal(err)
	}
	if fs.sent[0] != "raw\x01bytes" {
		t.Fatalf("sent %q", fs.sent[0])
	}
}

func TestSendAndExpectErrors(t *testing.T) {
	if _, err := SendAndExpect(context.Background(), &fakeSession{}, "x", ExpectOptions{ExpectRegex: "("}); err == nil {
		t.Fatal("expected regex compile error")
	}
	boom := errors.New("send")
	fs := &fakeSession{sendErr: boom, snapshots: []Snapshot{{Screen: ""}}}
	if _, err := SendAndExpect(context.Background(), fs, "x", ExpectOptions{}); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	wboom := errors.New("wait")
	fs2 := &fakeSession{waitErr: wboom, snapshots: []Snapshot{{Screen: ""}}}
	if _, err := SendAndExpect(context.Background(), fs2, "x", ExpectOptions{}); !errors.Is(err, wboom) {
		t.Fatalf("err = %v", err)
	}
	fs3 := &fakeSession{waitErr: wboom, snapshots: []Snapshot{{Screen: ""}}}
	if _, err := SendAndExpect(context.Background(), fs3, "x", ExpectOptions{ExpectText: "zz"}); !errors.Is(err, wboom) {
		t.Fatalf("err = %v", err)
	}
}

func TestSessionConnectedDefaultsTrue(t *testing.T) {
	type bare struct{ Session }
	if !sessionConnected(bare{}) {
		t.Fatal("sessions without IsConnected must default to connected")
	}
}

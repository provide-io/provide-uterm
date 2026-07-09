//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package session

import (
	"context"
	"regexp"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/sanitizer"
)

// ExpectResult is the outcome of SendAndExpect. Port of
// provide.uterm.expect.ExpectResult.
type ExpectResult struct {
	Matched     bool
	MatchedText string
	Screen      string
	TimedOut    bool
}

// ExpectOptions configure SendAndExpect.
type ExpectOptions struct {
	// ExpectText, when non-empty, matches by substring.
	ExpectText string
	// ExpectRegex, when non-empty, matches by regular expression.
	ExpectRegex string
	// Timeout is the total wait budget; zero selects 5s (the Python default).
	Timeout time.Duration
	// Sanitize runs the keys through sanitizer.PrepareKeystrokes; nil selects
	// the Python default of true.
	Sanitize *bool
}

// findMatch checks the substring and regex expectations. Unlike Python,
// where expect_text=None and expect_text="" differ, an empty ExpectText
// means "not provided" in the Go API.
func findMatch(screen, expectText string, expectRE *regexp.Regexp) (string, bool) {
	if expectText != "" && strings.Contains(screen, expectText) {
		return expectText, true
	}
	if expectRE != nil && expectRE.MatchString(screen) {
		return expectRE.FindString(screen), true
	}
	return "", false
}

// SendAndExpect sends keys and waits until the expected text or regex appears
// on screen. Port of provide.uterm.expect.send_and_expect.
func SendAndExpect(ctx context.Context, sess ExpectSession, keys string, opts ExpectOptions) (ExpectResult, error) {
	sanitize := true
	if opts.Sanitize != nil {
		sanitize = *opts.Sanitize
	}
	payload := keys
	if sanitize {
		payload = sanitizer.PrepareKeystrokes(keys, sanitizer.DefaultMaxBytes)
	}

	var expectRE *regexp.Regexp
	if opts.ExpectRegex != "" {
		re, err := regexp.Compile(opts.ExpectRegex)
		if err != nil {
			return ExpectResult{}, err
		}
		expectRE = re
	}

	timeout := opts.Timeout
	if timeout == 0 {
		timeout = 5 * time.Second
	}

	since := sess.ScreenChangeSeq()
	// An empty payload is a no-op write; skip it so callers can use this as a
	// pure read/wait without emitting a stray frame.
	if payload != "" {
		if err := sess.Send(ctx, payload); err != nil {
			return ExpectResult{}, err
		}
	}

	deadline := time.Now().Add(max(0, timeout))
	lastScreen := sess.Snapshot().Screen
	if matched, ok := findMatch(lastScreen, opts.ExpectText, expectRE); ok {
		return ExpectResult{Matched: true, MatchedText: matched, Screen: lastScreen}, nil
	}

	if opts.ExpectText == "" && expectRE == nil {
		remaining := max(0, time.Until(deadline))
		if _, err := sess.WaitForScreenChange(ctx, remaining, since); err != nil {
			return ExpectResult{}, err
		}
		return ExpectResult{Screen: sess.Snapshot().Screen}, nil
	}

	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return ExpectResult{Screen: lastScreen, TimedOut: true}, nil
		}
		changed, err := sess.WaitForScreenChange(ctx, max(time.Millisecond, remaining), since)
		if err != nil {
			return ExpectResult{}, err
		}
		lastScreen = sess.Snapshot().Screen
		if matched, ok := findMatch(lastScreen, opts.ExpectText, expectRE); ok {
			return ExpectResult{Matched: true, MatchedText: matched, Screen: lastScreen}, nil
		}
		if !changed {
			return ExpectResult{Screen: lastScreen, TimedOut: true}, nil
		}
		since = sess.ScreenChangeSeq()
	}
}

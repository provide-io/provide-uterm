//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"bytes"
	"io"
	"log/slog"
	"reflect"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// discardLogger returns a slog.Logger writing to io.Discard.
func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// captureLogger returns a logger writing into buf, plus a contains-check.
func captureLogger() (*slog.Logger, *bytes.Buffer) {
	buf := &bytes.Buffer{}
	return slog.New(slog.NewTextHandler(buf, nil)), buf
}

func logContains(s, substr string) bool {
	return strings.Contains(s, substr)
}

// decodeControlPayload decodes a single inline control frame, asserting exactly
// one control chunk (the Go analogue of the Python decode_control_payload).
func decodeControlPayload(t *testing.T, payload string) map[string]any {
	t.Helper()
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	events, err := dec.Feed(payload)
	if err != nil {
		t.Fatalf("feed: %v", err)
	}
	fin, err := dec.Finish()
	if err != nil {
		t.Fatalf("finish: %v", err)
	}
	events = append(events, fin...)
	var controls []map[string]any
	for _, e := range events {
		if c, ok := e.(controlchannel.ControlChunk); ok {
			controls = append(controls, c.Control)
		}
	}
	if len(controls) != 1 {
		t.Fatalf("expected exactly one control frame, got %d from %q", len(controls), payload)
	}
	return controls[0]
}

func mustEqual[T comparable](t *testing.T, got, want T, msg string) {
	t.Helper()
	if got != want {
		t.Fatalf("%s: got %v, want %v", msg, got, want)
	}
}

func mustDeepEqual(t *testing.T, got, want any, msg string) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s: got %#v, want %#v", msg, got, want)
	}
}

func mustTrue(t *testing.T, cond bool, msg string) {
	t.Helper()
	if !cond {
		t.Fatalf("expected true: %s", msg)
	}
}

func mustFalse(t *testing.T, cond bool, msg string) {
	t.Helper()
	if cond {
		t.Fatalf("expected false: %s", msg)
	}
}

// derefF64 returns the value or a sentinel NaN-ish marker for nil.
func f64OrNil(p *float64) (float64, bool) {
	if p == nil {
		return 0, false
	}
	return *p, true
}

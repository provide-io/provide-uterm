//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"reflect"
	"testing"
)

// The shim sends each write() the captured program makes as its own frame, and
// a program that flushes a buffer mid-character splits a UTF-8 sequence across
// two frames. These pin capture_connector.py's incremental decoding of them.

// pollStdout enqueues each chunk as its own stdout frame, polling after each,
// and returns the term data each poll produced ("" for a poll with none).
func pollStdout(c *CaptureConnector, chunks ...string) []string {
	out := make([]string, 0, len(chunks))
	for _, chunk := range chunks {
		c.capture.enqueue(CaptureFrame{Channel: ChannelStdout, Data: []byte(chunk)})
		data := ""
		for _, frame := range c.PollMessages() {
			data += frame["data"].(string)
		}
		out = append(out, data)
	}
	return out
}

func TestCaptureConnectorJoinsACharacterSplitAcrossFrames(t *testing.T) {
	c := startedCaptureConn(t)
	if got := pollStdout(c, "\xe2", "\x82\xac"); !reflect.DeepEqual(got, []string{"", "€"}) {
		t.Fatalf("term data = %q", got)
	}
	if c.buffer != "€" {
		t.Fatalf("buffer = %q", c.buffer)
	}
}

func TestCaptureConnectorJoinsEveryCutThroughAFourByteCharacter(t *testing.T) {
	data := "a😀b"
	for cut := 2; cut <= 4; cut++ {
		c := startedCaptureConn(t)
		got := pollStdout(c, data[:cut], data[cut:])
		if got[0]+got[1] != data || got[0] != "a" {
			t.Fatalf("cut %d: term data = %q", cut, got)
		}
	}
}

func TestCaptureConnectorJoinsACharacterSplitAcrossThreeFrames(t *testing.T) {
	c := startedCaptureConn(t)
	if got := pollStdout(c, "\xe2", "\x82", "\xac"); !reflect.DeepEqual(got, []string{"", "", "€"}) {
		t.Fatalf("term data = %q", got)
	}
}

func TestCaptureConnectorStillReplacesInvalidOutput(t *testing.T) {
	c := startedCaptureConn(t)
	if got := pollStdout(c, "a\xffb"); !reflect.DeepEqual(got, []string{"a\ufffdb"}) {
		t.Fatalf("term data = %q", got)
	}
}

func TestCaptureConnectorReplacesASequenceTheNextFrameDoesNotComplete(t *testing.T) {
	c := startedCaptureConn(t)
	if got := pollStdout(c, "\xe2\x82", "x"); !reflect.DeepEqual(got, []string{"", "\ufffdx"}) {
		t.Fatalf("term data = %q", got)
	}
}

func TestCaptureConnectorSessionsDecodeTheirOwnStreams(t *testing.T) {
	first, second := startedCaptureConn(t), startedCaptureConn(t)
	pollStdout(first, "\xe2\x82")
	if got := pollStdout(second, "z"); got[0] != "z" {
		t.Fatalf("second session term data = %q", got)
	}
	if got := pollStdout(first, "\xac"); got[0] != "€" {
		t.Fatalf("first session term data = %q", got)
	}
}

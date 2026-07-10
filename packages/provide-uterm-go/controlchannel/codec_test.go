//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlchannel

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
)

func mustEncode(t *testing.T, payload map[string]any) string {
	t.Helper()
	s, err := EncodeControlFrame(payload)
	if err != nil {
		t.Fatalf("EncodeControlFrame: %v", err)
	}
	return s
}

func feedAll(t *testing.T, d *Decoder, chunks ...string) []Chunk {
	t.Helper()
	var out []Chunk
	for _, c := range chunks {
		ev, err := d.Feed(c)
		if err != nil {
			t.Fatalf("Feed(%q): %v", c, err)
		}
		out = append(out, ev...)
	}
	return out
}

func TestEncodeTerminalDataEscapesDLE(t *testing.T) {
	if got := EncodeTerminalData("a\x10b"); got != "a\x10\x10b" {
		t.Fatalf("got %q", got)
	}
	if got := EncodeTerminalData("plain"); got != "plain" {
		t.Fatalf("got %q", got)
	}
}

func TestEncodeControlFrameShape(t *testing.T) {
	frame := mustEncode(t, map[string]any{"type": "ping"})
	want := "\x10\x02" + fmt.Sprintf("%08x", len(`{"type":"ping"}`)) + `:{"type":"ping"}`
	if frame != want {
		t.Fatalf("got %q want %q", frame, want)
	}
	if !IsControlFrame(frame) {
		t.Fatalf("IsControlFrame(%q) = false", frame)
	}
}

func TestEncodeControlFrameUnicodeByteLength(t *testing.T) {
	// Header must count UTF-8 bytes, not runes.
	frame := mustEncode(t, map[string]any{"msg": "héllo"})
	if !IsControlFrame(frame) {
		t.Fatalf("unicode frame not recognized: %q", frame)
	}
	d := NewDecoder(DecoderOptions{})
	events := feedAll(t, d, frame)
	if len(events) != 1 {
		t.Fatalf("events = %#v", events)
	}
	ctrl := events[0].(ControlChunk)
	if ctrl.Control["msg"] != "héllo" {
		t.Fatalf("payload = %#v", ctrl.Control)
	}
}

func TestEncodeControlFrameNoHTMLEscaping(t *testing.T) {
	frame := mustEncode(t, map[string]any{"cmd": "<a&b>"})
	if !strings.Contains(frame, "<a&b>") {
		t.Fatalf("HTML-escaped payload: %q", frame)
	}
}

func TestRoundTripMixedDataAndControl(t *testing.T) {
	frame := mustEncode(t, map[string]any{"type": "hello", "n": float64(1)})
	stream := EncodeTerminalData("before\x10dle") + frame + "after"
	d := NewDecoder(DecoderOptions{})
	events := feedAll(t, d, stream)
	if len(events) != 3 {
		t.Fatalf("events = %#v", events)
	}
	if got := events[0].(DataChunk).Data; got != "before\x10dle" {
		t.Fatalf("data[0] = %q", got)
	}
	ctrl := events[1].(ControlChunk)
	if ctrl.Control["type"] != "hello" || ctrl.Control["n"] != json.Number("1") {
		t.Fatalf("control = %#v", ctrl.Control)
	}
	if got := events[2].(DataChunk).Data; got != "after" {
		t.Fatalf("data[2] = %q", got)
	}
}

func TestKinds(t *testing.T) {
	if (DataChunk{}).Kind() != "data" || (ControlChunk{}).Kind() != "control" {
		t.Fatal("kind mismatch")
	}
}

func TestSplitFrameAcrossFeeds(t *testing.T) {
	frame := mustEncode(t, map[string]any{"type": "snapshot_req"})
	d := NewDecoder(DecoderOptions{})
	for cut := 1; cut < len(frame); cut++ {
		d2 := NewDecoder(DecoderOptions{})
		ev1, err := d2.Feed(frame[:cut])
		if err != nil {
			t.Fatalf("cut %d: %v", cut, err)
		}
		if len(ev1) != 0 {
			t.Fatalf("cut %d: early events %#v", cut, ev1)
		}
		ev2, err := d2.Feed(frame[cut:])
		if err != nil {
			t.Fatalf("cut %d: %v", cut, err)
		}
		if len(ev2) != 1 || ev2[0].(ControlChunk).Control["type"] != "snapshot_req" {
			t.Fatalf("cut %d: events %#v", cut, ev2)
		}
	}
	_ = d
}

func TestSplitEscapedDLEAcrossFeeds(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	ev, err := d.Feed("abc\x10")
	if err != nil || len(ev) != 1 || ev[0].(DataChunk).Data != "abc" {
		t.Fatalf("ev=%#v err=%v", ev, err)
	}
	ev, err = d.Feed("\x10def")
	if err != nil {
		t.Fatal(err)
	}
	if len(ev) != 1 || ev[0].(DataChunk).Data != "\x10def" {
		t.Fatalf("ev=%#v", ev)
	}
}

func TestFinishFlushesTrailingData(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	feedAll(t, d, "tail")
	ev, err := d.Finish()
	if err != nil {
		t.Fatal(err)
	}
	// "tail" has no DLE so Feed already emitted it; Finish yields nothing.
	if len(ev) != 0 {
		t.Fatalf("ev = %#v", ev)
	}
}

func TestFinishRejectsTruncatedFrame(t *testing.T) {
	frame := mustEncode(t, map[string]any{"type": "ping"})
	d := NewDecoder(DecoderOptions{})
	if _, err := d.Feed(frame[:len(frame)-2]); err != nil {
		t.Fatal(err)
	}
	_, err := d.Finish()
	var perr *ProtocolError
	if !errors.As(err, &perr) {
		t.Fatalf("err = %v", err)
	}
}

func TestFinishRejectsShortHeader(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	if _, err := d.Feed("\x10\x0200"); err != nil {
		t.Fatal(err)
	}
	_, err := d.Finish()
	if err == nil || err.Error() != "truncated control frame" {
		t.Fatalf("err = %v", err)
	}
}

func TestTrailingGarbageAfterJSON(t *testing.T) {
	payload := "{}garbage"
	frame := fmt.Sprintf("\x10\x02%08x:%s", len(payload), payload)
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed(frame)
	if err == nil || err.Error() != "invalid control json" {
		t.Fatalf("err = %v", err)
	}
}

func TestEncodeControlFrameUnserializable(t *testing.T) {
	_, err := EncodeControlFrame(map[string]any{"ch": make(chan int)})
	if err == nil {
		t.Fatal("expected marshal error")
	}
}

func TestFinishRejectsLoneDLE(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	if _, err := d.Feed("x\x10"); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Finish(); err == nil {
		t.Fatal("expected truncated control frame error")
	}
}

func TestInvalidControlPrefix(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed("\x10Zoops")
	if err == nil || err.Error() != "invalid control prefix" {
		t.Fatalf("err = %v", err)
	}
	// Buffer must be cleared after the error.
	ev, err := d.Feed("clean")
	if err != nil || len(ev) != 1 || ev[0].(DataChunk).Data != "clean" {
		t.Fatalf("ev=%#v err=%v", ev, err)
	}
}

func TestInvalidControlHeader(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed("\x10\x02zzzzzzzz:{}")
	if err == nil || err.Error() != "invalid control header" {
		t.Fatalf("err = %v", err)
	}
	d2 := NewDecoder(DecoderOptions{})
	_, err = d2.Feed("\x10\x0200000002X{}")
	if err == nil || err.Error() != "invalid control header" {
		t.Fatalf("err = %v", err)
	}
}

func TestPayloadTooLarge(t *testing.T) {
	d := NewDecoder(DecoderOptions{})
	header := fmt.Sprintf("\x10\x02%08x:", maxControlPayloadBytes+1)
	_, err := d.Feed(header)
	if err == nil || err.Error() != "control payload too large" {
		t.Fatalf("err = %v", err)
	}
	// A decoder-level cap below the protocol max also rejects.
	d2 := NewDecoder(DecoderOptions{MaxControlPayloadBytes: 4})
	frame := mustEncode(t, map[string]any{"type": "ping"})
	_, err = d2.Feed(frame)
	if err == nil || err.Error() != "control payload too large" {
		t.Fatalf("err = %v", err)
	}
}

func TestInvalidControlJSON(t *testing.T) {
	payload := "{not json}"
	frame := fmt.Sprintf("\x10\x02%08x:%s", len(payload), payload)
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed(frame)
	if err == nil || err.Error() != "invalid control json" {
		t.Fatalf("err = %v", err)
	}
}

func TestControlPayloadMustBeObject(t *testing.T) {
	payload := "[1,2,3]"
	frame := fmt.Sprintf("\x10\x02%08x:%s", len(payload), payload)
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed(frame)
	if err == nil || err.Error() != "control payload must be an object" {
		t.Fatalf("err = %v", err)
	}
}

func TestDepthLimit(t *testing.T) {
	deep := strings.Repeat(`{"a":`, 40) + "1" + strings.Repeat("}", 40)
	frame := fmt.Sprintf("\x10\x02%08x:%s", len(deep), deep)
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed(frame)
	if err == nil || !strings.Contains(err.Error(), "nests deeper than 32") {
		t.Fatalf("err = %v", err)
	}
	// Depth 32 passes (31 nested containers under the root).
	ok := strings.Repeat(`{"a":`, 31) + "1" + strings.Repeat("}", 31)
	frame = fmt.Sprintf("\x10\x02%08x:%s", len(ok), ok)
	d2 := NewDecoder(DecoderOptions{})
	ev, err := d2.Feed(frame)
	if err != nil || len(ev) != 1 {
		t.Fatalf("ev=%#v err=%v", ev, err)
	}
}

func TestDepthLimitCountsLists(t *testing.T) {
	deep := strings.Repeat("[", 40) + strings.Repeat("]", 40)
	payload := fmt.Sprintf(`{"a":%s}`, deep)
	frame := fmt.Sprintf("\x10\x02%08x:%s", len(payload), payload)
	d := NewDecoder(DecoderOptions{})
	if _, err := d.Feed(frame); err == nil {
		t.Fatal("expected depth error")
	}
}

func TestBufferOverflow(t *testing.T) {
	calls := 0
	d := NewDecoder(DecoderOptions{MaxBufferBytes: 8, OnError: func(code string) {
		calls++
		if code != "control_frame_protocol_error" {
			t.Fatalf("code = %q", code)
		}
	}})
	// An incomplete frame prefix stays buffered; the next feed overflows.
	if _, err := d.Feed("\x10\x020000"); err != nil {
		t.Fatal(err)
	}
	_, err := d.Feed("00000")
	if err == nil || !strings.Contains(err.Error(), "control frame buffer overflow") {
		t.Fatalf("err = %v", err)
	}
	if calls != 1 {
		t.Fatalf("onError calls = %d", calls)
	}
}

func TestPayloadLengthSplitsRune(t *testing.T) {
	// Declared length 1 byte, payload starts with a 2-byte rune.
	payload := "é"
	frame := "\x10\x0200000001:" + payload
	d := NewDecoder(DecoderOptions{})
	_, err := d.Feed(frame)
	if err == nil || err.Error() != "invalid control payload length" {
		t.Fatalf("err = %v", err)
	}
	if IsControlFrame(frame) {
		t.Fatal("IsControlFrame accepted split-rune payload")
	}
}

func TestIsControlFrame(t *testing.T) {
	frame := mustEncode(t, map[string]any{"type": "ping"})
	cases := []struct {
		in   string
		want bool
	}{
		{frame, true},
		{"", false},
		{"short", false},
		{frame + "trailing", false},
		{frame[:len(frame)-1], false},
		{"plain terminal data", false},
		{"\x10\x02XXXXXXXX:{}", false},
		{"\x10\x0200000002x{}", false},
		// Uppercase hex fails the canonical lowercase re-render check.
		{"\x10\x020000000A:" + `{"a":1234}`, false},
		{fmt.Sprintf("\x10\x02%08x:", maxControlPayloadBytes+1) + strings.Repeat("x", maxControlPayloadBytes+1), false},
	}
	for _, c := range cases {
		if got := IsControlFrame(c.in); got != c.want {
			t.Fatalf("IsControlFrame(%.30q) = %v want %v", c.in, got, c.want)
		}
	}
}

func TestConsecutiveFrames(t *testing.T) {
	f1 := mustEncode(t, map[string]any{"type": "ping"})
	f2 := mustEncode(t, map[string]any{"type": "pong"})
	d := NewDecoder(DecoderOptions{})
	ev := feedAll(t, d, f1+f2)
	if len(ev) != 2 {
		t.Fatalf("ev = %#v", ev)
	}
	if ev[0].(ControlChunk).Control["type"] != "ping" || ev[1].(ControlChunk).Control["type"] != "pong" {
		t.Fatalf("ev = %#v", ev)
	}
}

func TestOnErrorFiredOncePerError(t *testing.T) {
	calls := 0
	d := NewDecoder(DecoderOptions{OnError: func(string) { calls++ }})
	_, err := d.Feed("\x10Z")
	if err == nil {
		t.Fatal("expected error")
	}
	if calls != 1 {
		t.Fatalf("calls = %d", calls)
	}
}

func TestWSBytesRoundTrip(t *testing.T) {
	raw := make([]byte, 256)
	for i := range raw {
		raw[i] = byte(i)
	}
	s := WSBytesToChannelStr(raw)
	back := ChannelStrToBytes(s)
	if string(back) != string(raw) {
		t.Fatal("latin-1 round trip lost bytes")
	}
	// Non-latin-1 codepoints are replaced with '?'.
	if got := ChannelStrToBytes("a→b"); string(got) != "a?b" {
		t.Fatalf("got %q", got)
	}
}

func TestDataThroughChannelPreservesHighBytes(t *testing.T) {
	// CP437 box drawing bytes 0xB0-0xDF must survive encode→decode→bytes.
	raw := []byte{0xC9, 0xCD, 0xBB, 0x10, 0xC8, 0xBC}
	channel := EncodeTerminalData(WSBytesToChannelStr(raw))
	d := NewDecoder(DecoderOptions{})
	ev, err := d.Feed(channel)
	if err != nil || len(ev) != 1 {
		t.Fatalf("ev=%#v err=%v", ev, err)
	}
	back := ChannelStrToBytes(ev[0].(DataChunk).Data)
	if string(back) != string(raw) {
		t.Fatalf("got %v want %v", back, raw)
	}
}

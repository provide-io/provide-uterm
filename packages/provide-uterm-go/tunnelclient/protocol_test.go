//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// goldenFile pins Python-generated wire frames. Regenerate with the scratchpad
// generator described in its _note field.
const goldenFile = "testdata/frames_golden.json"

type goldenDoc struct {
	Frames []struct {
		Channel    int    `json:"channel"`
		Flags      int    `json:"flags"`
		PayloadB64 string `json:"payload_b64"`
		FrameB64   string `json:"frame_b64"`
	} `json:"frames"`
	Controls []struct {
		Kind     string `json:"kind"`
		Cols     int    `json:"cols"`
		Rows     int    `json:"rows"`
		Port     int    `json:"port"`
		FrameB64 string `json:"frame_b64"`
	} `json:"controls"`
}

func loadGolden(t *testing.T) goldenDoc {
	t.Helper()
	raw, err := os.ReadFile(goldenFile)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var doc goldenDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	if len(doc.Frames) == 0 || len(doc.Controls) == 0 {
		t.Fatal("golden missing frames/controls")
	}
	return doc
}

func mustB64(t *testing.T, s string) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatalf("bad base64 %q: %v", s, err)
	}
	return b
}

// TestFrameGoldenParity proves Go's EncodeFrame produces byte-identical wire
// frames to Python's encode_frame for a committed corpus, and that DecodeFrame
// round-trips them (both directions of the differential test).
func TestFrameGoldenParity(t *testing.T) {
	for _, c := range loadGolden(t).Frames {
		payload := mustB64(t, c.PayloadB64)
		wantFrame := mustB64(t, c.FrameB64)

		gotFrame := EncodeFrame(byte(c.Channel), payload, byte(c.Flags))
		if !bytes.Equal(gotFrame, wantFrame) {
			t.Fatalf("EncodeFrame(ch=%d,fl=%d) = %x, Python = %x", c.Channel, c.Flags, gotFrame, wantFrame)
		}

		// Python frame → Go decode → Go re-encode must reproduce the same bytes.
		dec, err := DecodeFrame(wantFrame)
		if err != nil {
			t.Fatalf("DecodeFrame: %v", err)
		}
		if int(dec.Channel) != c.Channel || int(dec.Flags) != c.Flags {
			t.Fatalf("decoded ch=%d fl=%d, want ch=%d fl=%d", dec.Channel, dec.Flags, c.Channel, c.Flags)
		}
		if !bytes.Equal(dec.Payload, payload) {
			t.Fatalf("decoded payload = %x, want %x", dec.Payload, payload)
		}
		reenc := EncodeFrame(dec.Channel, dec.Payload, dec.Flags)
		if !bytes.Equal(reenc, wantFrame) {
			t.Fatalf("re-encode = %x, want %x", reenc, wantFrame)
		}
	}
}

// TestControlGoldenParity proves the order-preserving control builders match
// Python's encode_control byte-for-byte.
func TestControlGoldenParity(t *testing.T) {
	for _, c := range loadGolden(t).Controls {
		var got []byte
		switch c.Kind {
		case "open_terminal":
			got = OpenTerminalFrame(c.Cols, c.Rows)
		case "resize":
			got = ResizeFrame(c.Cols, c.Rows)
		case "open_tcp":
			got = OpenTCPFrame(c.Port)
		case "open_http":
			got = OpenHTTPFrame(c.Port)
		default:
			t.Fatalf("unknown control kind %q", c.Kind)
		}
		if want := mustB64(t, c.FrameB64); !bytes.Equal(got, want) {
			t.Fatalf("%s frame = %x, Python = %x", c.Kind, got, want)
		}
	}
}

func TestDecodeFrameTooShort(t *testing.T) {
	if _, err := DecodeFrame([]byte{0x01}); err == nil {
		t.Fatal("expected error for 1-byte frame")
	}
	if _, err := DecodeFrame(nil); err == nil {
		t.Fatal("expected error for empty frame")
	}
}

func TestFrameHelpers(t *testing.T) {
	f := Frame{Channel: ChannelControl, Flags: FlagEOF, Payload: nil}
	if !f.IsEOF() {
		t.Fatal("IsEOF should be true")
	}
	if !f.IsControl() {
		t.Fatal("IsControl should be true")
	}
	d := Frame{Channel: ChannelData, Flags: FlagData}
	if d.IsEOF() || d.IsControl() {
		t.Fatal("data frame should be neither EOF nor control")
	}
}

func TestEncodeControl(t *testing.T) {
	if _, err := EncodeControl(map[string]any{"no": "type"}); err == nil {
		t.Fatal("expected error when type key missing")
	}
	frame, err := EncodeControl(map[string]any{"type": "ping"})
	if err != nil {
		t.Fatalf("EncodeControl: %v", err)
	}
	if frame[0] != ChannelControl || frame[1] != FlagData {
		t.Fatalf("control frame header = %x %x", frame[0], frame[1])
	}
	obj, err := DecodeControl(frame[2:])
	if err != nil {
		t.Fatalf("DecodeControl: %v", err)
	}
	if obj["type"] != "ping" {
		t.Fatalf("round-trip type = %v", obj["type"])
	}
	if _, err := DecodeControl([]byte("not json")); err == nil {
		t.Fatal("expected error decoding invalid control payload")
	}
	// A value json cannot marshal (a channel) surfaces the marshal-error branch.
	if _, err := EncodeControl(map[string]any{"type": "x", "bad": make(chan int)}); err == nil {
		t.Fatal("expected marshal error for unmarshalable control value")
	}
}

// TestFrameParityLive differential-tests against the live Python encoder when
// uv/python is available, covering payloads beyond the frozen golden.
func TestFrameParityLive(t *testing.T) {
	cases := []struct {
		channel, flags byte
		payload        []byte
	}{
		{ChannelData, FlagData, []byte("live-diff \x00\xff\xfe end")},
		{ChannelHTTP, FlagEOF, nil},
		{0x2A, 0x80, bytes.Repeat([]byte{0xAB}, 300)},
	}
	for _, c := range cases {
		pyFrame, ok := pythonEncodeFrame(t, c.channel, c.flags, c.payload)
		if !ok {
			t.Skip("uv/python unavailable; golden test covers parity")
		}
		got := EncodeFrame(c.channel, c.payload, c.flags)
		if !bytes.Equal(got, pyFrame) {
			t.Fatalf("live parity ch=%d fl=%d: Go %x != Python %x", c.channel, c.flags, got, pyFrame)
		}
	}
}

// pythonEncodeFrame calls Python's encode_frame and returns the raw bytes.
func pythonEncodeFrame(t *testing.T, channel, flags byte, payload []byte) ([]byte, bool) {
	t.Helper()
	script := "import sys,base64;from provide.uterm.tunnel.protocol import encode_frame;" +
		"ch=int(sys.argv[1]);fl=int(sys.argv[2]);pl=base64.b64decode(sys.argv[3]);" +
		"sys.stdout.write(base64.b64encode(encode_frame(ch,pl,flags=fl)).decode())"
	cmd := exec.Command("uv", "run", "python", "-c", script,
		itoa(int(channel)), itoa(int(flags)), base64.StdEncoding.EncodeToString(payload))
	cmd.Dir = "../../provide-uterm-server"
	out, err := cmd.Output()
	if err != nil {
		return nil, false
	}
	b, derr := base64.StdEncoding.DecodeString(strings.TrimSpace(string(out)))
	if derr != nil {
		return nil, false
	}
	return b, true
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [4]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

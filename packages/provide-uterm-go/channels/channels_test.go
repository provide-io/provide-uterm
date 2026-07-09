//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package channels

import (
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

func mustNegotiated(t *testing.T, supported map[string]int, def string) *Negotiated {
	t.Helper()
	n, err := NewNegotiated(supported, def)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestNewNegotiatedValidation(t *testing.T) {
	if _, err := NewNegotiated(map[string]int{}, ""); err == nil {
		t.Fatal("empty supported must error")
	}
	if _, err := NewNegotiated(map[string]int{"": 1}, ""); err == nil {
		t.Fatal("empty channel name must error")
	}
	if _, err := NewNegotiated(map[string]int{"a": 1}, "missing"); err == nil {
		t.Fatal("unsupported default must error")
	}
}

func TestHandleHelloNegotiatesMinVersions(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"term": 3, "inspect": 1}, "term")
	ack, err := n.HandleHello(Hello{Channels: map[string]int{"term": 5, "inspect": 1, "unknown": 2, "zero": 0}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if ack["type"] != "hello_ack" {
		t.Fatalf("ack = %#v", ack)
	}
	granted := ack["channels"].(map[string]int)
	if granted["term"] != 3 || granted["inspect"] != 1 {
		t.Fatalf("granted = %#v", granted)
	}
	if _, ok := granted["unknown"]; ok {
		t.Fatal("unknown channel granted")
	}
	if _, ok := granted["zero"]; ok {
		t.Fatal("zero-version channel granted")
	}
	ok, err := n.IsNegotiated("")
	if err != nil || !ok {
		t.Fatalf("default channel not negotiated: %v", err)
	}
	ok, err = n.IsNegotiated("unknown")
	if err != nil || ok {
		t.Fatalf("unknown negotiated: %v", err)
	}
}

func TestHandleHelloAckFields(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"term": 1}, "")
	ack, err := n.HandleHello(Hello{Channels: map[string]int{"term": 1}}, map[string]any{"role": "operator"})
	if err != nil || ack["role"] != "operator" {
		t.Fatalf("ack=%#v err=%v", ack, err)
	}
	for _, reserved := range []string{"type", "channels"} {
		if _, err := n.HandleHello(Hello{}, map[string]any{reserved: 1}); err == nil ||
			!strings.Contains(err.Error(), "reserved hello_ack field: "+reserved) {
			t.Fatalf("reserved %q: err=%v", reserved, err)
		}
	}
}

func TestReservedFieldReportsFirstSorted(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"term": 1}, "")
	_, err := n.HandleHello(Hello{}, map[string]any{"type": 1, "channels": 2})
	if err == nil || !strings.Contains(err.Error(), "reserved hello_ack field: channels") {
		t.Fatalf("err = %v", err)
	}
}

func TestNextSeqPerChannel(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"a": 1, "b": 1}, "a")
	for want := 1; want <= 3; want++ {
		got, err := n.NextSeq("")
		if err != nil || got != want {
			t.Fatalf("seq = %d want %d (%v)", got, want, err)
		}
	}
	got, err := n.NextSeq("b")
	if err != nil || got != 1 {
		t.Fatalf("b seq = %d (%v)", got, err)
	}
}

func TestNextSeqRequiresChannelWithoutDefault(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"a": 1}, "")
	if _, err := n.NextSeq(""); err == nil {
		t.Fatal("expected error without default channel")
	}
	if _, err := n.IsNegotiated(""); err == nil {
		t.Fatal("expected error without default channel")
	}
}

func TestExportRestoreGrants(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"term": 2}, "term")
	if _, err := n.HandleHello(Hello{Channels: map[string]int{"term": 2}}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := n.NextSeq(""); err != nil {
		t.Fatal(err)
	}
	grants := n.ExportGrants()
	n2 := mustNegotiated(t, map[string]int{"term": 2}, "term")
	restore := make(map[string]any, len(grants))
	for k, v := range grants {
		restore[k] = float64(v) // as decoded from persisted JSON
	}
	if err := n2.RestoreGrants(restore); err != nil {
		t.Fatal(err)
	}
	if n2.Granted()["term"] != 2 {
		t.Fatalf("granted = %#v", n2.Granted())
	}
	seq, err := n2.NextSeq("")
	if err != nil || seq != 1 {
		t.Fatalf("seq = %d (%v)", seq, err)
	}
}

func TestRestoreGrantsRejectsBadValues(t *testing.T) {
	n := mustNegotiated(t, map[string]int{"term": 2}, "term")
	cases := []map[string]any{
		nil,
		{"term": true},
		{"term": "1"},
		{"term": 1.5},
		{"": 1},
	}
	for _, grants := range cases {
		if err := n.RestoreGrants(grants); err == nil {
			t.Fatalf("grants %#v accepted", grants)
		}
	}
	// Native ints are accepted too.
	if err := n.RestoreGrants(map[string]any{"term": 2}); err != nil {
		t.Fatal(err)
	}
}

func helloFrame(t *testing.T, payload map[string]any) string {
	t.Helper()
	frame, err := controlchannel.EncodeControlFrame(payload)
	if err != nil {
		t.Fatal(err)
	}
	return frame
}

func TestParseChannelHello(t *testing.T) {
	frame := helloFrame(t, map[string]any{"type": "hello", "channels": map[string]any{"term": 1.0}})
	hello := ParseChannelHello(frame)
	if hello == nil || hello.Channels["term"] != 1 {
		t.Fatalf("hello = %#v", hello)
	}
}

func TestDecodeFramesErrorPaths(t *testing.T) {
	// Feed error: structurally valid frame but bad JSON payload.
	bad := "\x10\x02" + "0000000a" + ":{not json}"
	if got := decodeFrames(bad); got != nil {
		t.Fatalf("decodeFrames = %#v", got)
	}
	if got := ParseChannelHello(bad); got != nil {
		t.Fatalf("ParseChannelHello = %#v", got)
	}
	// Finish error: trailing truncated frame after data.
	if got := decodeFrames("data\x10\x0200"); got != nil {
		t.Fatalf("decodeFrames = %#v", got)
	}
}

func TestParseChannelHelloRejects(t *testing.T) {
	cases := []string{
		"",
		"not a frame",
		helloFrame(t, map[string]any{"type": "term", "data": "x"}),                               // wrong type
		helloFrame(t, map[string]any{"type": "hello"}),                                           // missing channels
		helloFrame(t, map[string]any{"type": "hello", "channels": "nope"}),                       // non-mapping
		helloFrame(t, map[string]any{"type": "hello", "channels": map[string]any{"term": true}}), // bool version
	}
	for _, raw := range cases {
		if got := ParseChannelHello(raw); got != nil {
			t.Fatalf("ParseChannelHello(%.40q) = %#v", raw, got)
		}
	}
}

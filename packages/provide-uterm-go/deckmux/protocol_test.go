//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import "testing"

func TestMessageTypeConstants(t *testing.T) {
	got := map[string]string{
		MsgPresenceUpdate: "presence_update", MsgPresenceSync: "presence_sync",
		MsgPresenceLeave: "presence_leave", MsgControlTransfer: "control_transfer",
		MsgQueuedInput: "queued_input", MsgControlRequest: "control_request",
		MsgAutoTransferWarning: "auto_transfer_warning",
	}
	for k, v := range got {
		if k != v {
			t.Errorf("constant %q != %q", k, v)
		}
	}
}

// TestGoldenEncodeKeys checks EncodeKeysDisplay against the Python outputs.
func TestGoldenEncodeKeys(t *testing.T) {
	var cases []struct {
		Raw     string `json:"raw"`
		Display string `json:"display"`
	}
	goldenCase(t, "encode_keys", &cases)
	if len(cases) == 0 {
		t.Fatal("no encode_keys golden")
	}
	for _, c := range cases {
		if got := EncodeKeysDisplay(c.Raw); got != c.Display {
			t.Errorf("EncodeKeysDisplay(%q) = %q, want %q", c.Raw, got, c.Display)
		}
	}
}

func TestEncodeKeysDisplaySpotChecks(t *testing.T) {
	cases := map[string]string{
		"\x1b[A": "↑", "\r": "↵", "\t": "⇥", "hello": "hello",
		"ls\r": "ls↵", "\x1b[Ahello\x1b[D": "↑hello←",
		"\x01": "", "a\x02b": "ab", "": "", "\x1b[": "⎋[",
	}
	for raw, want := range cases {
		if got := EncodeKeysDisplay(raw); got != want {
			t.Errorf("EncodeKeysDisplay(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestMakePresenceUpdate(t *testing.T) {
	msg := MakePresenceUpdate("u1", "Alice", "#fff", "admin", nil)
	want := map[string]any{
		"type": "presence_update", "user_id": "u1", "name": "Alice",
		"color": "#fff", "role": "admin",
	}
	for k, v := range want {
		if msg[k] != v {
			t.Errorf("msg[%q] = %v, want %v", k, msg[k], v)
		}
	}
	if len(msg) != len(want) {
		t.Errorf("minimal msg has %d keys, want %d", len(msg), len(want))
	}

	msg2 := MakePresenceUpdate("u1", "Alice", "#fff", "admin", map[string]any{
		"scroll_line": 42, "typing": true, "is_owner": true, "unknown_field": "ignored",
	})
	if msg2["scroll_line"] != 42 || msg2["typing"] != true || msg2["is_owner"] != true {
		t.Error("optional fields not copied")
	}
	if _, ok := msg2["unknown_field"]; ok {
		t.Error("unknown field must not be copied")
	}
}

func TestMakePresenceSyncLeaveTransfer(t *testing.T) {
	sync := MakePresenceSync([]map[string]any{{"user_id": "u1"}}, map[string]any{"k": 1})
	if sync["type"] != "presence_sync" {
		t.Error("sync type")
	}
	leave := MakePresenceLeave("u1")
	if leave["type"] != "presence_leave" || leave["user_id"] != "u1" || len(leave) != 2 {
		t.Error("leave shape")
	}
	ct := MakeControlTransfer("u1", "u2", "handover", "")
	if ct["from_user_id"] != "u1" || ct["to_user_id"] != "u2" ||
		ct["reason"] != "handover" || ct["queued_keys"] != "" {
		t.Error("control_transfer shape")
	}
}

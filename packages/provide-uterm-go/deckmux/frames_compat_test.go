//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"encoding/json"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// TestFramesPackageCompatibility proves the DeckMux wire builders produce JSON
// that round-trips through the frames package's discriminated-union decoder
// into the matching PresenceUpdateFrame / PresenceSyncFrame / PresenceLeaveFrame
// / ControlTransferFrame structs (the extra="allow" ones carry the presence
// fields in Extra; the extra="forbid" ones must decode with no unknown keys).
func TestFramesPackageCompatibility(t *testing.T) {
	cases := []struct {
		msg      map[string]any
		wantType string
	}{
		{MakePresenceUpdate("u1", "Alice", "#fff", "admin", map[string]any{"scroll_line": 5, "typing": true}), frames.TypePresenceUpdate},
		{MakePresenceSync([]map[string]any{{"user_id": "u1"}}, map[string]any{"k": 1}), frames.TypePresenceSync},
		{MakePresenceLeave("u1"), frames.TypePresenceLeave},
		{MakeControlTransfer("u1", "u2", "handover", "ls"), frames.TypeControlTransfer},
	}
	for _, c := range cases {
		data, err := json.Marshal(c.msg)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		decoded, err := frames.DecodeFrame(data)
		if err != nil {
			t.Fatalf("frames.DecodeFrame(%s): %v", data, err)
		}
		f, ok := decoded.(frames.Frame)
		if !ok {
			t.Fatalf("decoded %T is not a frames.Frame", decoded)
		}
		if f.FrameType() != c.wantType {
			t.Fatalf("decoded to %q, want %q", f.FrameType(), c.wantType)
		}
	}

	// Spot-check that the extra="allow" PresenceUpdateFrame preserves the
	// presence fields in Extra and the typed user_id.
	data, _ := json.Marshal(MakePresenceUpdate("u1", "Alice", "#fff", "admin", map[string]any{"scroll_line": 5}))
	decoded, _ := frames.DecodeFrame(data)
	pu := decoded.(*frames.PresenceUpdateFrame)
	if pu.UserID == nil || *pu.UserID != "u1" {
		t.Errorf("user_id not decoded: %+v", pu)
	}
	if pu.Extra["name"] != "Alice" || pu.Extra["scroll_line"].(float64) != 5 {
		t.Errorf("extras not preserved: %v", pu.Extra)
	}

	// And that the forbid-policy ControlTransferFrame decodes its typed fields.
	data, _ = json.Marshal(MakeControlTransfer("a", "b", "handover", "keys"))
	decoded, _ = frames.DecodeFrame(data)
	ct := decoded.(*frames.ControlTransferFrame)
	if ct.FromUserID == nil || *ct.FromUserID != "a" || ct.QueuedKeys == nil || *ct.QueuedKeys != "keys" {
		t.Errorf("control_transfer fields: %+v", ct)
	}
}

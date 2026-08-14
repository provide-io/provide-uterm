//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import (
	"encoding/json"
	"os"
	"reflect"
	"slices"
	"testing"
)

// testdata/python_golden.json holds wire JSON produced by the actual Python
// builders (provide.uterm.server.bridge.frames + provide.uterm.
// control_channel_builders). Regenerate from the repo root with:
//
//	uv run python <scratch>/gen_golden.py > frames/testdata/python_golden.json
//
// (see the script header for its exact location and contents).
//
// Comparison is semantic: both sides are parsed and compared as maps, so key
// order is irrelevant. The Python builders make_snapshot_frame,
// make_analysis_frame and make_hijack_state_frame dump exclude_none=False
// and emit explicit nulls for unset optionals; the Go marshalers omit those
// keys, so null-valued keys are stripped from the golden before comparing —
// and each stripped key must appear in wantNullStripped so the deviation
// stays visible.

// goldenGoFrames maps each golden case to the Go frame that must produce
// semantically-equal wire JSON.
func goldenGoFrames() map[string]Frame {
	// WithDefaults, not the bare builder: spec/behavior.json pins go's
	// hello_defaults at mcp_supported=true, vnc_supported=true, and a server
	// hello is exactly what this golden represents. The bare MakeHelloFrame
	// matched only because this corpus predates 5145daae (capability
	// negotiation, 2026-07-19) and nothing regenerated it.
	hello := MakeHelloFrameWithDefaults()
	hello.WorkerID = Ptr("w1")
	hello.CanHijack = Ptr(true)
	hello.Hijacked = Ptr(false)
	hello.WorkerOnline = Ptr(true)
	hello.InputMode = Ptr("raw")
	hello.Protocol = map[string]int{"selected": 2, "server_min": 1, "server_max": 2}

	identityFull := NewIdentityFrame("user:alice")
	identityFull.Claims = map[string]any{"role": "admin", "n": 3}
	identityFull.Fingerprint = "SHA256:fp"
	identityFull.Transport = "ws"

	return map[string]Frame{
		"error":               MakeErrorFrame("boom"),
		"pong":                MakePongFrame(123.5),
		"heartbeat_ack":       MakeHeartbeatAckFrame(456.25, 123.5),
		"worker_connected":    MakeWorkerConnectedFrame("w1", 1.5),
		"worker_disconnected": MakeWorkerDisconnectedFrame("w1", 2.5),
		"term":                MakeTermFrame("hi\x1b[0mé", 3.5),
		"snapshot_minimal": MakeSnapshotFrame(SnapshotParams{
			Screen:           "line1\nline2",
			Cursor:           map[string]int{"x": 1, "y": 2},
			Cols:             80,
			Rows:             25,
			ScreenHash:       "abc123",
			CursorAtEnd:      true,
			HasTrailingSpace: false,
			PromptDetected:   nil,
			TS:               9.5,
			RawTail:          nil,
		}),
		"snapshot_full": MakeSnapshotFrame(SnapshotParams{
			Screen:           "s",
			Cursor:           map[string]int{"x": 0, "y": 0},
			Cols:             132,
			Rows:             43,
			ScreenHash:       "h",
			CursorAtEnd:      false,
			HasTrailingSpace: true,
			PromptDetected:   map[string]any{"prompt_id": "shell", "confidence": 0.75},
			TS:               10.5,
			RawTail:          Ptr("tail\x1b[1m"),
		}),
		"analysis_null_raw": MakeAnalysisFrame("f", nil, 4.5),
		"analysis_raw":      MakeAnalysisFrame("f", map[string]any{"k": []any{1, 2}}, 5.5),
		"hijack_state_off":  MakeHijackStateFrame(false, nil, nil, "raw"),
		"hijack_state_on":   MakeHijackStateFrame(true, Ptr("alice"), Ptr(99.5), "cooked"),
		"hello":             hello,
		"status":            CoerceWorkerStatusFrame(map[string]any{"cpu": 12.5, "tag": "ok", "ts": 6.5}),
		"identity_defaults": NewIdentityFrame("user:bob"),
		"identity_full":     identityFull,
		"session_token": SessionTokenFrame{
			Type: TypeSessionToken, Token: "tok", PlayerID: Ptr(3),
		},
		"session_token_no_player": SessionTokenFrame{Type: TypeSessionToken, Token: "tok2"},
		"resume":                  ResumeFrame{Type: TypeResume, Token: "rtok", PlayerID: Ptr(7)},
		"resume_ok":               ResumeOkFrame{Type: TypeResumeOk},
		"resume_failed":           ResumeFailedFrame{Type: TypeResumeFailed, Reason: Ptr("expired")},
		"link_patterns": LinkPatternsFrame{
			Type: TypeLinkPatterns,
			Patterns: []LinkPatternEntry{
				{Pattern: `foo(\d+)`, Action: "cmd", ID: Ptr("p1"), Group: 1, Payload: "run {1}"},
				{
					Pattern:      `https?://\S+`,
					Action:       "url",
					Flags:        Ptr("i"),
					Hover:        Ptr("open"),
					LineContains: Ptr("http"),
					Class:        Ptr("link"),
				},
			},
		},
		"presence_update": PresenceUpdateFrame{
			Type:   TypePresenceUpdate,
			UserID: Ptr("u1"),
			Extra:  map[string]any{"scroll_line": 5, "typing": true},
		},
	}
}

// wantNullStripped lists, per golden case, the keys the Python builder emits
// as explicit null (exclude_none=False) that the Go marshaler omits.
var wantNullStripped = map[string][]string{
	// bytes_read/chunks_read arrive as explicit nulls from the Python builder
	// (SnapshotFrame in bridge/schemas.py); the Go marshaler omits them, being
	// nil pointers with omitzero -- the same treatment prompt_detected and
	// raw_tail already get. Added when the corpus was re-recorded: it had been
	// stale since 2a6e9dbb introduced the counters, and that staleness was ALSO
	// hiding that SnapshotFrame carried no fields for them at all, so decoding a
	// real snapshot failed outright on an unknown field.
	"snapshot_minimal":  {"bytes_read", "chunks_read", "prompt_detected", "raw_tail"},
	"snapshot_full":     {"bytes_read", "chunks_read"},
	"analysis_null_raw": {"raw"},
	"hijack_state_off":  {"lease_expires_at", "owner"},
}

// stripNulls removes top-level null-valued keys, returning the sorted list of
// stripped keys.
func stripNulls(m map[string]any) []string {
	var stripped []string
	for k, v := range m {
		if v == nil {
			delete(m, k)
			stripped = append(stripped, k)
		}
	}
	slices.Sort(stripped)
	return stripped
}

func loadGolden(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile("testdata/python_golden.json")
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var golden map[string]json.RawMessage
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	return golden
}

func TestGoldenAgainstPythonBuilders(t *testing.T) {
	golden := loadGolden(t)
	goFrames := goldenGoFrames()
	if len(golden) != len(goFrames) {
		t.Fatalf("golden has %d cases, Go table has %d", len(golden), len(goFrames))
	}
	for name, frame := range goFrames {
		t.Run(name, func(t *testing.T) {
			rawGolden, ok := golden[name]
			if !ok {
				t.Fatalf("golden case %q missing", name)
			}
			var want map[string]any
			if err := json.Unmarshal(rawGolden, &want); err != nil {
				t.Fatalf("parse golden case: %v", err)
			}
			stripped := stripNulls(want)
			if !slices.Equal(stripped, wantNullStripped[name]) {
				t.Fatalf("null-stripped keys %v, want %v", stripped, wantNullStripped[name])
			}
			data, err := EncodeFrame(frame)
			if err != nil {
				t.Fatalf("EncodeFrame: %v", err)
			}
			var got map[string]any
			if err := json.Unmarshal(data, &got); err != nil {
				t.Fatalf("parse Go output: %v", err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("wire mismatch:\n  go:     %s\n  python: %s", data, rawGolden)
			}
		})
	}
}

func TestGoldenFramesDecode(t *testing.T) {
	// Every Python-built frame (including the exclude_none=False ones with
	// explicit nulls) must decode cleanly and dispatch to the right struct.
	golden := loadGolden(t)
	for name, raw := range golden {
		t.Run(name, func(t *testing.T) {
			got, err := DecodeFrame(raw)
			if err != nil {
				t.Fatalf("DecodeFrame: %v", err)
			}
			var head struct {
				Type string `json:"type"`
			}
			if err := json.Unmarshal(raw, &head); err != nil {
				t.Fatalf("parse type: %v", err)
			}
			f, ok := got.(Frame)
			if !ok {
				t.Fatalf("decoded %T is not a Frame", got)
			}
			if f.FrameType() != head.Type {
				t.Fatalf("dispatched to %T (literal %q), want type %q", got, f.FrameType(), head.Type)
			}
		})
	}
}

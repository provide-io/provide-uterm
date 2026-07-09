//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

// extraPolicy mirrors the Pydantic model_config extra=... policy.
type extraPolicy int

const (
	policyForbid extraPolicy = iota
	policyIgnore
	policyAllow
)

// roundTripCases covers every frame type of the AnyFrame union, with minimal
// and fully-populated variants. All any/map values use JSON-native Go types
// (float64, string, bool, map[string]any, []any) so decode(encode(x)) == x.
var roundTripCases = []struct {
	name   string
	frame  Frame
	policy extraPolicy
}{
	{"term", TermFrame{Type: TypeTerm, Data: "hi\x1b[0m", TS: Ptr(1.5)}, policyForbid},
	{"term_no_ts", TermFrame{Type: TypeTerm, Data: ""}, policyForbid},
	{"input", InputFrame{Type: TypeInput, Data: "ls\n", TS: Ptr(2.5)}, policyForbid},
	{"snapshot_req", SnapshotReqFrame{Type: TypeSnapshotReq, TS: Ptr(3.5)}, policyForbid},
	{"snapshot_minimal", SnapshotFrame{Type: TypeSnapshot, Screen: "s"}, policyForbid},
	{
		"snapshot_full",
		SnapshotFrame{
			Type:             TypeSnapshot,
			Screen:           "a\nb",
			Cursor:           map[string]int{"x": 1, "y": 2},
			Cols:             Ptr(80),
			Rows:             Ptr(25),
			ScreenHash:       Ptr("h"),
			CursorAtEnd:      Ptr(true),
			HasTrailingSpace: Ptr(false),
			PromptDetected:   map[string]any{"prompt_id": "shell", "confidence": 0.5},
			RawTail:          Ptr("tail"),
			TS:               Ptr(4.5),
		},
		policyForbid,
	},
	{
		"control",
		ControlFrame{Type: TypeControl, Action: "pause", Owner: Ptr("o"), LeaseS: Ptr(30.0), TS: Ptr(5.5)},
		policyForbid,
	},
	{"hijack_state_min", HijackStateFrame{Type: TypeHijackState, Hijacked: false}, policyForbid},
	{
		"hijack_state_full",
		HijackStateFrame{
			Type:           TypeHijackState,
			Hijacked:       true,
			Owner:          Ptr("alice"),
			LeaseExpiresAt: Ptr(99.5),
			InputMode:      Ptr("raw"),
		},
		policyForbid,
	},
	{"hijack_request", HijackRequestFrame{Type: TypeHijackRequest, Token: Ptr("t"), TS: Ptr(6.5)}, policyForbid},
	{"hijack_release", HijackReleaseFrame{Type: TypeHijackRelease, TS: Ptr(7.5)}, policyForbid},
	{"hijack_step", HijackStepFrame{Type: TypeHijackStep}, policyForbid},
	{"worker_connected", WorkerConnectedFrame{Type: TypeWorkerConnected, WorkerID: "w1", TS: Ptr(8.5)}, policyForbid},
	{
		"worker_disconnected",
		WorkerDisconnectedFrame{Type: TypeWorkerDisconnected, WorkerID: "w1", TS: Ptr(9.5)},
		policyForbid,
	},
	{"worker_hello", WorkerHelloFrame{Type: TypeWorkerHello, Mode: Ptr("raw"), TS: Ptr(10.5)}, policyForbid},
	{"heartbeat", HeartbeatFrame{Type: TypeHeartbeat, TS: Ptr(11.5)}, policyForbid},
	{"heartbeat_ack", HeartbeatAckFrame{Type: TypeHeartbeatAck, LeaseExpiresAt: 0, TS: Ptr(12.5)}, policyForbid},
	{"ping", PingFrame{Type: TypePing, TS: Ptr(13.5)}, policyForbid},
	{"pong", PongFrame{Type: TypePong}, policyForbid},
	{"hello_min", HelloFrame{Type: TypeHello}, policyIgnore},
	{
		"hello_full",
		HelloFrame{
			Type:                TypeHello,
			WorkerID:            Ptr("w1"),
			CanHijack:           Ptr(true),
			Hijacked:            Ptr(false),
			HijackedByMe:        Ptr(false),
			WorkerOnline:        Ptr(true),
			InputMode:           Ptr("raw"),
			Role:                Ptr("operator"),
			HijackControl:       Ptr("lease"),
			HijackStepSupported: Ptr(true),
			Capabilities:        map[string]any{"inspect": true},
			ResumeSupported:     Ptr(true),
			ResumeToken:         Ptr("rt"),
			Resumed:             Ptr(false),
			ProtocolVersion:     Ptr(2),
			Protocol:            map[string]int{"selected": 2, "server_min": 1, "server_max": 2},
			TS:                  Ptr(14.5),
		},
		policyIgnore,
	},
	{"resume", ResumeFrame{Type: TypeResume, Token: "tok", PlayerID: Ptr(2)}, policyForbid},
	{"identity_min", NewIdentityFrame("user:bob"), policyAllow},
	{
		"identity_full",
		IdentityFrame{
			Type:        TypeIdentity,
			Version:     2,
			Subject:     "user:alice",
			Fingerprint: "SHA256:fp",
			Transport:   "ws",
			Claims:      map[string]any{"role": "admin"},
			Signature:   Ptr("sig"),
			Extra:       map[string]any{"custom": "x"},
		},
		policyAllow,
	},
	{"session_token", SessionTokenFrame{Type: TypeSessionToken, Token: "tok", PlayerID: Ptr(3)}, policyForbid},
	{"resume_ok", ResumeOkFrame{Type: TypeResumeOk}, policyForbid},
	{"resume_failed", ResumeFailedFrame{Type: TypeResumeFailed, Reason: Ptr("expired")}, policyForbid},
	{
		"link_patterns",
		LinkPatternsFrame{
			Type: TypeLinkPatterns,
			Patterns: []LinkPatternEntry{
				{Pattern: `foo(\d+)`, Action: "cmd", ID: Ptr("p1"), Group: float64(1), Payload: "run {1}"},
				{
					Pattern:      `https?://\S+`,
					Action:       "url",
					Flags:        Ptr("i"),
					Group:        "g",
					Hover:        Ptr("open"),
					LineContains: Ptr("http"),
					Class:        Ptr("link"),
				},
			},
		},
		policyForbid,
	},
	{
		"analysis",
		AnalysisFrame{Type: TypeAnalysis, Formatted: "f", Raw: map[string]any{"k": []any{"a", "b"}}, TS: Ptr(15.5)},
		policyForbid,
	},
	{"analysis_no_raw", AnalysisFrame{Type: TypeAnalysis, Formatted: "f"}, policyForbid},
	{
		"error_full",
		ErrorFrame{
			Type:      TypeError,
			Message:   "boom",
			Reason:    Ptr("protocol_mismatch"),
			ClientMin: Ptr(1),
			ClientMax: Ptr(2),
			ServerMin: Ptr(3),
			ServerMax: Ptr(4),
		},
		policyForbid,
	},
	{"error_min", ErrorFrame{Type: TypeError, Message: ""}, policyForbid},
	{"status_min", StatusFrame{Type: TypeStatus}, policyAllow},
	{
		"status_full",
		StatusFrame{Type: TypeStatus, TS: Ptr(16.5), Extra: map[string]any{"cpu": 12.5, "tag": "ok"}},
		policyAllow,
	},
	{"input_mode_changed", InputModeChangedFrame{Type: TypeInputModeChanged, InputMode: "cooked", TS: Ptr(17.5)}, policyForbid},
	{
		"approval_pending",
		ApprovalPendingFrame{Type: TypeApprovalPending, Command: "rm -rf /", RequestID: "r1", ExpiresAt: 0},
		policyForbid,
	},
	{"approval_resolved", ApprovalResolvedFrame{Type: TypeApprovalResolved, Outcome: "denied", RequestID: "r1"}, policyForbid},
	{"presence_update_min", PresenceUpdateFrame{Type: TypePresenceUpdate}, policyAllow},
	{
		"presence_update_full",
		PresenceUpdateFrame{
			Type:   TypePresenceUpdate,
			UserID: Ptr("u1"),
			Extra:  map[string]any{"typing": true, "scroll_line": 5.0},
		},
		policyAllow,
	},
	{"presence_sync_min", PresenceSyncFrame{Type: TypePresenceSync}, policyAllow},
	{
		"presence_sync_full",
		PresenceSyncFrame{
			Type:    TypePresenceSync,
			Users:   []map[string]any{{"id": "u1"}, {"id": "u2"}},
			Config:  map[string]any{"max": 4.0},
			OwnerID: Ptr("u1"),
			Extra:   map[string]any{"epoch": 3.0},
		},
		policyAllow,
	},
	{"presence_leave", PresenceLeaveFrame{Type: TypePresenceLeave, UserID: "u1", TS: Ptr(18.5)}, policyForbid},
	{
		"control_transfer",
		ControlTransferFrame{
			Type:       TypeControlTransfer,
			FromUserID: Ptr("a"),
			ToUserID:   Ptr("b"),
			Reason:     Ptr("idle"),
			QueuedKeys: Ptr("qq"),
		},
		policyForbid,
	},
}

// derefDecoded unwraps the *T returned by DecodeFrame back to T.
func derefDecoded(t *testing.T, got any) any {
	t.Helper()
	rv := reflect.ValueOf(got)
	if rv.Kind() != reflect.Pointer || rv.IsNil() {
		t.Fatalf("DecodeFrame returned %T, want non-nil pointer", got)
	}
	return rv.Elem().Interface()
}

func TestRoundTripEveryFrameType(t *testing.T) {
	seen := map[string]bool{}
	for _, tc := range roundTripCases {
		t.Run(tc.name, func(t *testing.T) {
			data, err := EncodeFrame(tc.frame)
			if err != nil {
				t.Fatalf("EncodeFrame: %v", err)
			}
			got, err := DecodeFrame(data)
			if err != nil {
				t.Fatalf("DecodeFrame(%s): %v", data, err)
			}
			deref := derefDecoded(t, got)
			if !reflect.DeepEqual(deref, tc.frame) {
				t.Fatalf("round trip mismatch:\n got %#v\nwant %#v\nwire %s", deref, tc.frame, data)
			}
		})
		seen[tc.frame.FrameType()] = true
	}
	if len(seen) != len(decoders) {
		t.Fatalf("round-trip table covers %d frame types, want %d", len(seen), len(decoders))
	}
}

func TestUnknownFieldPolicy(t *testing.T) {
	for _, tc := range roundTripCases {
		t.Run(tc.name, func(t *testing.T) {
			data, err := EncodeFrame(tc.frame)
			if err != nil {
				t.Fatalf("EncodeFrame: %v", err)
			}
			var m map[string]any
			if err := json.Unmarshal(data, &m); err != nil {
				t.Fatalf("unmarshal: %v", err)
			}
			m["bogus__"] = 1.25
			mutated, err := json.Marshal(m)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			got, err := DecodeFrame(mutated)
			switch tc.policy {
			case policyForbid:
				if err == nil || !strings.Contains(err.Error(), "bogus__") {
					t.Fatalf("forbid model accepted unknown field (err=%v)", err)
				}
			case policyIgnore:
				if err != nil {
					t.Fatalf("ignore model rejected unknown field: %v", err)
				}
				if deref := derefDecoded(t, got); !reflect.DeepEqual(deref, tc.frame) {
					t.Fatalf("ignore model changed by unknown field:\n got %#v\nwant %#v", deref, tc.frame)
				}
			case policyAllow:
				if err != nil {
					t.Fatalf("allow model rejected unknown field: %v", err)
				}
				extra := reflect.ValueOf(got).Elem().FieldByName("Extra").Interface().(map[string]any)
				if extra["bogus__"] != 1.25 {
					t.Fatalf("allow model lost extra field: Extra=%v", extra)
				}
				delete(extra, "bogus__")
				if len(extra) == 0 {
					reflect.ValueOf(got).Elem().FieldByName("Extra").Set(reflect.Zero(reflect.TypeOf(extra)))
				}
				if deref := derefDecoded(t, got); !reflect.DeepEqual(deref, tc.frame) {
					t.Fatalf("allow model mismatch after extras strip:\n got %#v\nwant %#v", deref, tc.frame)
				}
			}
		})
	}
}

func TestDiscriminatorDispatch(t *testing.T) {
	for _, tc := range roundTripCases {
		data, err := EncodeFrame(tc.frame)
		if err != nil {
			t.Fatalf("%s: EncodeFrame: %v", tc.name, err)
		}
		got, err := DecodeFrame(data)
		if err != nil {
			t.Fatalf("%s: DecodeFrame: %v", tc.name, err)
		}
		if reflect.TypeOf(derefDecoded(t, got)) != reflect.TypeOf(tc.frame) {
			t.Fatalf("%s: dispatched to %T, want %T", tc.name, got, tc.frame)
		}
	}
}

func TestDecodeFrameErrors(t *testing.T) {
	cases := []struct {
		name string
		data string
	}{
		{"invalid_json", "not json"},
		{"json_array", "[1,2]"},
		{"non_string_type", `{"type":5}`},
		{"unknown_type", `{"type":"nope"}`},
		{"missing_type", `{"data":"x"}`},
		{"forbid_bad_field_type", `{"type":"term","data":5}`},
		{"ignore_bad_field_type", `{"type":"hello","ts":"x"}`},
		{"allow_identity_bad_field", `{"type":"identity","version":"x"}`},
		{"allow_status_bad_field", `{"type":"status","ts":"x"}`},
		{"allow_presence_update_bad_field", `{"type":"presence_update","user_id":5}`},
		{"allow_presence_sync_bad_field", `{"type":"presence_sync","users":5}`},
		{"nested_unknown_field", `{"type":"link_patterns","patterns":[{"pattern":"p","action":"cmd","bogus":1}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got, err := DecodeFrame([]byte(tc.data)); err == nil {
				t.Fatalf("DecodeFrame(%q) = %#v, want error", tc.data, got)
			}
		})
	}
}

func TestEncodeFrameValidation(t *testing.T) {
	if _, err := EncodeFrame(42); err == nil {
		t.Fatal("EncodeFrame(42) should fail")
	}
	if _, err := EncodeFrame(TermFrame{Type: "input", Data: "x"}); err == nil {
		t.Fatal("EncodeFrame with wrong type literal should fail")
	}
	if _, err := EncodeFrame(TermFrame{Data: "x"}); err == nil {
		t.Fatal("EncodeFrame with empty type literal should fail")
	}
	if _, err := EncodeFrame((*TermFrame)(nil)); err == nil {
		t.Fatal("EncodeFrame(nil pointer) should fail")
	}
	// Pointer form is accepted.
	data, err := EncodeFrame(&PingFrame{Type: TypePing})
	if err != nil {
		t.Fatalf("EncodeFrame pointer: %v", err)
	}
	if string(data) != `{"type":"ping"}` {
		t.Fatalf("got %s", data)
	}
	// A marshal failure inside an allow-model surfaces as an error.
	if _, err := EncodeFrame(StatusFrame{Type: TypeStatus, Extra: map[string]any{"bad": make(chan int)}}); err == nil {
		t.Fatal("EncodeFrame with unmarshalable extra should fail")
	}
}

func TestRequiredZeroValuesSerialize(t *testing.T) {
	cases := []struct {
		frame Frame
		want  string
	}{
		{HijackStateFrame{Type: TypeHijackState, Hijacked: false}, `{"hijacked":false,"type":"hijack_state"}`},
		{HeartbeatAckFrame{Type: TypeHeartbeatAck, LeaseExpiresAt: 0}, `{"lease_expires_at":0,"type":"heartbeat_ack"}`},
		{TermFrame{Type: TypeTerm, Data: ""}, `{"data":"","type":"term"}`},
	}
	for _, tc := range cases {
		data, err := EncodeFrame(tc.frame)
		if err != nil {
			t.Fatalf("EncodeFrame(%#v): %v", tc.frame, err)
		}
		var got, want map[string]any
		if err := json.Unmarshal(data, &got); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if err := json.Unmarshal([]byte(tc.want), &want); err != nil {
			t.Fatalf("unmarshal want: %v", err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("got %s want %s", data, tc.want)
		}
	}
}

func TestIdentityDecodeDefaults(t *testing.T) {
	got, err := DecodeFrame([]byte(`{"type":"identity","subject":"user:bob"}`))
	if err != nil {
		t.Fatalf("DecodeFrame: %v", err)
	}
	f, ok := got.(*IdentityFrame)
	if !ok {
		t.Fatalf("got %T", got)
	}
	want := NewIdentityFrame("user:bob")
	if !reflect.DeepEqual(*f, want) {
		t.Fatalf("defaults not applied:\n got %#v\nwant %#v", *f, want)
	}
	// Explicit values are honored over defaults.
	got, err = DecodeFrame([]byte(`{"type":"identity","subject":"s","version":3,"fingerprint":"fp","transport":"ws"}`))
	if err != nil {
		t.Fatalf("DecodeFrame: %v", err)
	}
	f = got.(*IdentityFrame)
	if f.Version != 3 || f.Fingerprint != "fp" || f.Transport != "ws" {
		t.Fatalf("explicit fields lost: %#v", f)
	}
}

func TestExtractExtrasInvalidJSON(t *testing.T) {
	if got := extractExtras([]byte("not json"), "type"); got != nil {
		t.Fatalf("got %v, want nil", got)
	}
}

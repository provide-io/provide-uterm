//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import (
	"reflect"
	"testing"
	"time"
)

// tsIsNow asserts a builder-stamped timestamp is "current" (ts <= 0 path).
func tsIsNow(t *testing.T, ts *float64) {
	t.Helper()
	if ts == nil {
		t.Fatal("ts not stamped")
	}
	now := float64(time.Now().UnixNano()) / 1e9
	// Two-sided window: the stamp must be neither stale nor implausibly far in
	// the future. A one-sided (lower-bound-only) check lets a mangled unit
	// conversion (e.g. nowTS multiplying instead of dividing by 1e9) survive.
	if *ts <= 0 || now-*ts > 60 || *ts-now > 60 {
		t.Fatalf("ts %v is not current (now %v)", *ts, now)
	}
}

func TestMakeErrorFrame(t *testing.T) {
	want := ErrorFrame{Type: TypeError, Message: "boom"}
	if got := MakeErrorFrame("boom"); !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestMakePongFrame(t *testing.T) {
	if got := MakePongFrame(123.5); *got.TS != 123.5 || got.Type != TypePong {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakePongFrame(0).TS)
	tsIsNow(t, MakePongFrame(-1).TS)
}

func TestMakeHeartbeatAckFrame(t *testing.T) {
	got := MakeHeartbeatAckFrame(456.25, 123.5)
	if got.Type != TypeHeartbeatAck || got.LeaseExpiresAt != 456.25 || *got.TS != 123.5 {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakeHeartbeatAckFrame(1, 0).TS)
}

func TestMakeWorkerConnectedFrame(t *testing.T) {
	got := MakeWorkerConnectedFrame("w1", 1.5)
	if got.Type != TypeWorkerConnected || got.WorkerID != "w1" || *got.TS != 1.5 {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakeWorkerConnectedFrame("w1", 0).TS)
}

func TestMakeWorkerDisconnectedFrame(t *testing.T) {
	got := MakeWorkerDisconnectedFrame("w1", 2.5)
	if got.Type != TypeWorkerDisconnected || got.WorkerID != "w1" || *got.TS != 2.5 {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakeWorkerDisconnectedFrame("w1", 0).TS)
}

func TestMakeTermFrame(t *testing.T) {
	got := MakeTermFrame("data", 3.5)
	if got.Type != TypeTerm || got.Data != "data" || *got.TS != 3.5 {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakeTermFrame("d", 0).TS)
}

func TestMakeSnapshotFrame(t *testing.T) {
	got := MakeSnapshotFrame(SnapshotParams{
		Screen:           "s",
		Cursor:           map[string]int{"x": 1, "y": 2},
		Cols:             80,
		Rows:             25,
		ScreenHash:       "h",
		CursorAtEnd:      true,
		HasTrailingSpace: false,
		PromptDetected:   map[string]any{"prompt_id": "shell"},
		TS:               9.5,
		RawTail:          Ptr("tail"),
	})
	want := SnapshotFrame{
		Type:             TypeSnapshot,
		Screen:           "s",
		Cursor:           map[string]int{"x": 1, "y": 2},
		Cols:             Ptr(80),
		Rows:             Ptr(25),
		ScreenHash:       Ptr("h"),
		CursorAtEnd:      Ptr(true),
		HasTrailingSpace: Ptr(false),
		PromptDetected:   map[string]any{"prompt_id": "shell"},
		RawTail:          Ptr("tail"),
		TS:               Ptr(9.5),
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	tsIsNow(t, MakeSnapshotFrame(SnapshotParams{Screen: "s"}).TS)
}

func TestMakeAnalysisFrame(t *testing.T) {
	got := MakeAnalysisFrame("f", map[string]any{"k": "v"}, 4.5)
	if got.Type != TypeAnalysis || got.Formatted != "f" || *got.TS != 4.5 {
		t.Fatalf("got %#v", got)
	}
	if !reflect.DeepEqual(got.Raw, map[string]any{"k": "v"}) {
		t.Fatalf("raw %#v", got.Raw)
	}
	nilRaw := MakeAnalysisFrame("f", nil, 0)
	if nilRaw.Raw != nil {
		t.Fatalf("raw %#v", nilRaw.Raw)
	}
	tsIsNow(t, nilRaw.TS)
}

func TestMakeHijackStateFrame(t *testing.T) {
	got := MakeHijackStateFrame(true, Ptr("alice"), Ptr(99.5), "raw")
	want := HijackStateFrame{
		Type:           TypeHijackState,
		Hijacked:       true,
		Owner:          Ptr("alice"),
		LeaseExpiresAt: Ptr(99.5),
		InputMode:      Ptr("raw"),
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	off := MakeHijackStateFrame(false, nil, nil, "cooked")
	if off.Hijacked || off.Owner != nil || off.LeaseExpiresAt != nil || *off.InputMode != "cooked" {
		t.Fatalf("got %#v", off)
	}
}

func TestMakeHelloFrame(t *testing.T) {
	if got := MakeHelloFrame(); !reflect.DeepEqual(got, HelloFrame{Type: TypeHello}) {
		t.Fatalf("got %#v", got)
	}
}

func TestNewIdentityFrame(t *testing.T) {
	got := NewIdentityFrame("user:bob")
	want := IdentityFrame{
		Type:        TypeIdentity,
		Version:     1,
		Subject:     "user:bob",
		Fingerprint: "",
		Transport:   "ssh",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestCoerceWorkerStatusFrame(t *testing.T) {
	t.Run("float_ts_and_extras", func(t *testing.T) {
		got := CoerceWorkerStatusFrame(map[string]any{"cpu": 12.5, "ts": 6.5})
		want := StatusFrame{Type: TypeStatus, TS: Ptr(6.5), Extra: map[string]any{"cpu": 12.5}}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("got %#v", got)
		}
	})
	t.Run("int_ts", func(t *testing.T) {
		got := CoerceWorkerStatusFrame(map[string]any{"ts": 7})
		if *got.TS != 7 || got.Extra != nil {
			t.Fatalf("got %#v", got)
		}
	})
	t.Run("defaults_ts_to_now", func(t *testing.T) {
		got := CoerceWorkerStatusFrame(map[string]any{})
		if got.Type != TypeStatus || got.Extra != nil {
			t.Fatalf("got %#v", got)
		}
		tsIsNow(t, got.TS)
	})
	t.Run("string_type_kept", func(t *testing.T) {
		if got := CoerceWorkerStatusFrame(map[string]any{"type": "status"}); got.Type != "status" {
			t.Fatalf("got %#v", got)
		}
	})
	t.Run("non_string_type_to_extra", func(t *testing.T) {
		got := CoerceWorkerStatusFrame(map[string]any{"type": 5.0})
		if got.Type != TypeStatus || got.Extra["type"] != 5.0 {
			t.Fatalf("got %#v", got)
		}
	})
	t.Run("non_numeric_ts_to_extra", func(t *testing.T) {
		got := CoerceWorkerStatusFrame(map[string]any{"ts": "weird"})
		if got.TS != nil || got.Extra["ts"] != "weird" {
			t.Fatalf("got %#v", got)
		}
	})
}

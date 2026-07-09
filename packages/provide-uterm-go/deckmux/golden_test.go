//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// testdata/python_golden.json holds wire payloads produced by the real Python
// DeckMux. Regenerate from the repo root with:
//
//	uv run python <scratch>/gen_deckmux_golden.py > \
//	  packages/provide-uterm-go/deckmux/testdata/python_golden.json
//
// Comparison is semantic: both sides are parsed to Go values and compared as
// maps/slices, so JSON key order is irrelevant.

// golden is the parsed golden document, loaded once.
var golden = loadGoldenDoc()

func loadGoldenDoc() map[string]json.RawMessage {
	raw, err := os.ReadFile("testdata/python_golden.json")
	if err != nil {
		panic(err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(raw, &doc); err != nil {
		panic(err)
	}
	return doc
}

// goldenCase unmarshals a named golden case into v.
func goldenCase(t *testing.T, name string, v any) {
	t.Helper()
	raw, ok := golden[name]
	if !ok {
		t.Fatalf("golden case %q missing", name)
	}
	if err := json.Unmarshal(raw, v); err != nil {
		t.Fatalf("unmarshal golden %q: %v", name, err)
	}
}

// mustJSON marshals v to a JSON string (map keys sorted deterministically).
func mustJSON(t *testing.T, v any) string {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(data)
}

// jsonEqual reports whether the Go value marshals to the same JSON structure as
// the named golden case (order-insensitive map compare).
func jsonEqual(t *testing.T, name string, gotValue any) {
	t.Helper()
	data, err := json.Marshal(gotValue)
	if err != nil {
		t.Fatalf("marshal got: %v", err)
	}
	var got any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("re-parse got: %v", err)
	}
	var want any
	goldenCase(t, name, &want)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("wire mismatch for %q:\n  go:     %s\n  python: %s", name, data, golden[name])
	}
}

// TestGoldenProtocolBuilders checks the four wire builders + presence shapes
// against the Python builders byte-for-byte (map-compare level).
func TestGoldenProtocolBuilders(t *testing.T) {
	jsonEqual(t, "presence_update_min", MakePresenceUpdate("u1", "Alice", "#fff", "admin", nil))
	jsonEqual(t, "presence_update_full", MakePresenceUpdate("u1", "Alice", "#fff", "admin", map[string]any{
		"scroll_line": 42, "scroll_range": []int{0, 100}, "selection": map[string]any{"start": 0},
		"pin": map[string]any{"line": 5}, "typing": true, "queued_keys": "ls", "is_owner": true,
	}))
	jsonEqual(t, "presence_sync", MakePresenceSync(
		[]map[string]any{{"user_id": "u1", "name": "Alice"}}, map[string]any{"idle_timeout": 30}))
	jsonEqual(t, "presence_leave", MakePresenceLeave("u1"))
	jsonEqual(t, "control_transfer_min", MakeControlTransfer("u1", "u2", "handover", ""))
	jsonEqual(t, "control_transfer_queued", MakeControlTransfer("u1", "u2", "auto_idle", "ls\r"))
}

// TestGoldenToDict checks UserPresence.ToDict against Python UserPresence.to_dict.
func TestGoldenToDict(t *testing.T) {
	jsonEqual(t, "to_dict_default",
		UserPresence{UserID: "u1", Name: "Alice", Color: "#fff", Role: "admin"}.ToDict())
	jsonEqual(t, "to_dict_full", UserPresence{
		UserID: "u1", Name: "Alice", Color: "#fff", Role: "admin", Initials: "AL",
		ScrollLine: 7, ScrollRange: []int{3, 27}, TotalLines: 99,
		Selection: map[string]any{"start": map[string]any{"row": 1, "col": 2}},
		Pin:       map[string]any{"row": 4, "col": 5},
		Typing:    true, QueuedKeys: "ls↵", Cols: 132, Rows: 43, IsOwner: true,
	}.ToDict())
}

// TestGoldenSyncPayload checks PresenceStore.GetSyncPayload (insertion order).
func TestGoldenSyncPayload(t *testing.T) {
	store := NewPresenceStore()
	store.Add("sre:alice", "Alice", "#e74c3c", "operator", "AL")
	store.Add("sre:bob", "Bob", "#3498db", "viewer", "BO")
	jsonEqual(t, "sync_payload", store.GetSyncPayload(
		map[string]any{"auto_transfer_idle_s": 30, "keystroke_queue": "display"}))
}

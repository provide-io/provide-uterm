//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"reflect"
	"strings"
	"testing"
)

// withTimeNow temporarily overrides the package clock (not for use in parallel
// tests), mirroring the Python suite patching _presence.time.
func withTimeNow(t *testing.T, now float64) {
	t.Helper()
	prev := timeNow
	timeNow = func() float64 { return now }
	t.Cleanup(func() { timeNow = prev })
}

func TestUserPresenceDefaults(t *testing.T) {
	p := UserPresence{UserID: "u1", Name: "Alice", Color: "#fff", Role: "admin"}
	if p.ScrollLine != 0 || p.ScrollRange != nil || p.Selection != nil || p.Pin != nil ||
		p.Typing || p.QueuedKeys != "" || p.IsOwner || p.Initials != "" {
		t.Error("unexpected non-zero default")
	}
}

func TestUserPresenceToDict(t *testing.T) {
	p := UserPresence{UserID: "u1", Name: "Alice", Color: "#fff", Role: "admin", Initials: "AL"}
	d := p.ToDict()
	if d["user_id"] != "u1" || d["name"] != "Alice" || d["initials"] != "AL" || d["is_owner"] != false {
		t.Error("to_dict scalar fields")
	}
	if !reflect.DeepEqual(d["scroll_range"], []int{0, 0}) {
		t.Errorf("scroll_range default = %v", d["scroll_range"])
	}
	// A nil map stored in an `any` is a non-nil interface wrapping a nil map;
	// it marshals to JSON null (covered by the golden). Assert the nil map.
	if sel, ok := d["selection"].(map[string]any); !ok || sel != nil {
		t.Error("selection default must be a nil map")
	}
	if pin, ok := d["pin"].(map[string]any); !ok || pin != nil {
		t.Error("pin default must be a nil map")
	}
}

func TestUserPresenceToDictWithSelectionPin(t *testing.T) {
	p := UserPresence{
		UserID: "u1", Name: "A", Color: "#000", Role: "viewer",
		Selection:   map[string]any{"start": 1, "end": 5},
		Pin:         map[string]any{"line": 10},
		ScrollRange: []int{2, 8},
	}
	d := p.ToDict()
	if !reflect.DeepEqual(d["selection"], map[string]any{"start": 1, "end": 5}) {
		t.Error("selection")
	}
	if !reflect.DeepEqual(d["scroll_range"], []int{2, 8}) {
		t.Error("scroll_range explicit")
	}
}

func TestUserPresenceIsIdle(t *testing.T) {
	withTimeNow(t, 1000.0)
	if !(UserPresence{LastActivityAt: 940.0}).IsIdle(30.0) {
		t.Error("should be idle")
	}
	if (UserPresence{LastActivityAt: 1000.0}).IsIdle(30.0) {
		t.Error("should not be idle")
	}
}

func TestStoreAddGetCount(t *testing.T) {
	s := NewPresenceStore()
	if s.Count() != 0 {
		t.Error("empty count")
	}
	p := s.Add("u1", "Alice", "#fff", "admin", "AL")
	if p.UserID != "u1" || p.Initials != "AL" {
		t.Error("add return")
	}
	got, ok := s.Get("u1")
	if !ok || got.UserID != "u1" {
		t.Error("get")
	}
	if s.Count() != 1 {
		t.Error("count 1")
	}
	if _, ok := s.Get("nope"); ok {
		t.Error("get missing must be false")
	}
}

func TestStoreAddDefaultInitialsEmpty(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	u, _ := s.Get("u1")
	if u.Initials != "" {
		t.Errorf("default initials = %q", u.Initials)
	}
}

func TestStoreUpdate(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	withTimeNow(t, 999.0)
	u, ok, err := s.Update("u1", map[string]any{"typing": true, "scroll_line": 42})
	if err != nil || !ok {
		t.Fatalf("update err=%v ok=%v", err, ok)
	}
	if !u.Typing || u.ScrollLine != 42 || u.LastActivityAt != 999.0 {
		t.Errorf("update result %+v", u)
	}
}

func TestStoreUpdateMissing(t *testing.T) {
	s := NewPresenceStore()
	_, ok, err := s.Update("nope", map[string]any{"typing": true})
	if ok || err != nil {
		t.Errorf("missing update ok=%v err=%v", ok, err)
	}
}

func TestStoreUpdateUnknownField(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	_, _, err := s.Update("u1", map[string]any{"nonexistent_field": "value"})
	if err == nil || !strings.Contains(err.Error(), "nonexistent_field") {
		t.Errorf("expected unknown-field error, got %v", err)
	}
}

func TestStoreUpdateSelectionPin(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")

	sel := map[string]any{"start": map[string]any{"row": 1, "col": 2}, "end": map[string]any{"row": 3, "col": 4}}
	u, ok, err := s.Update("u1", map[string]any{"selection": sel})
	if err != nil || !ok || !reflect.DeepEqual(u.Selection, sel) {
		t.Errorf("valid selection: %v %v %+v", err, ok, u.Selection)
	}
	if _, _, err := s.Update("u1", map[string]any{"pin": map[string]any{"line": 10}}); err != nil {
		t.Errorf("valid pin: %v", err)
	}
	// nil clears both.
	u, _, err = s.Update("u1", map[string]any{"selection": nil, "pin": nil})
	if err != nil || u.Selection != nil || u.Pin != nil {
		t.Errorf("nil clear: %v %+v", err, u)
	}
}

func TestStoreUpdateSelectionRejected(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")

	big := map[string]any{"blob": strings.Repeat("x", 4096)}
	_, _, err := s.Update("u1", map[string]any{"selection": big})
	if err == nil || !strings.Contains(err.Error(), "invalid presence selection") {
		t.Errorf("oversized selection err = %v", err)
	}
	// Must not mutate stored state.
	if u, _ := s.Get("u1"); u.Selection != nil {
		t.Error("rejected update mutated state")
	}

	if _, _, err := s.Update("u1", map[string]any{"pin": big}); err == nil ||
		!strings.Contains(err.Error(), "invalid presence pin") {
		t.Errorf("oversized pin err = %v", err)
	}

	many := make(map[string]any, 17)
	for i := 0; i < 17; i++ {
		many[strings.Repeat("k", i+1)] = i
	}
	if _, _, err := s.Update("u1", map[string]any{"selection": many}); err == nil ||
		!strings.Contains(err.Error(), "too many keys") {
		t.Errorf("too many keys err = %v", err)
	}

	if _, _, err := s.Update("u1", map[string]any{"selection": "x"}); err == nil ||
		!strings.Contains(err.Error(), "must be a dict") {
		t.Errorf("non-dict selection err = %v", err)
	}
	if _, _, err := s.Update("u1", map[string]any{"pin": []any{1, 2}}); err == nil ||
		!strings.Contains(err.Error(), "invalid presence pin") {
		t.Errorf("non-dict pin err = %v", err)
	}
}

func TestStoreUpdateExactlyMaxKeys(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	exactly := make(map[string]any, maxPresenceDictKeys)
	for i := 0; i < maxPresenceDictKeys; i++ {
		exactly[strings.Repeat("k", i+1)] = i
	}
	if _, ok, err := s.Update("u1", map[string]any{"selection": exactly}); err != nil || !ok {
		t.Errorf("exactly max keys rejected: %v", err)
	}
}

func TestStoreRemove(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	removed, ok := s.Remove("u1")
	if !ok || removed.UserID != "u1" || s.Count() != 0 {
		t.Error("remove")
	}
	if _, ok := s.Get("u1"); ok {
		t.Error("still present after remove")
	}
	if _, ok := s.Remove("nope"); ok {
		t.Error("remove missing")
	}
}

func TestStoreGetAll(t *testing.T) {
	s := NewPresenceStore()
	if len(s.GetAll()) != 0 {
		t.Error("empty get_all")
	}
	s.Add("u1", "Alice", "#fff", "admin", "")
	s.Add("u2", "Bob", "#000", "viewer", "")
	all := s.GetAll()
	if len(all) != 2 || all[0].UserID != "u1" || all[1].UserID != "u2" {
		t.Errorf("get_all order/len: %+v", all)
	}
}

func TestStoreOwner(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	s.Add("u2", "Bob", "#000", "viewer", "")
	if _, ok := s.GetOwner(); ok {
		t.Error("no owner initially")
	}
	s.SetOwner("u1")
	owner, ok := s.GetOwner()
	if !ok || owner.UserID != "u1" {
		t.Error("get owner")
	}
	u1, _ := s.Get("u1")
	u2, _ := s.Get("u2")
	if !u1.IsOwner || u2.IsOwner {
		t.Error("owner flags")
	}
	s.SetOwner("u2") // clears previous
	u1, _ = s.Get("u1")
	u2, _ = s.Get("u2")
	if u1.IsOwner || !u2.IsOwner {
		t.Error("owner transfer")
	}
	s.ClearOwner()
	if _, ok := s.GetOwner(); ok {
		t.Error("clear owner")
	}
}

func TestStoreSyncPayload(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	payload := s.GetSyncPayload(map[string]any{"idle_timeout": 30})
	if payload["type"] != "presence_sync" {
		t.Error("sync type")
	}
	users := payload["users"].([]map[string]any)
	if len(users) != 1 || users[0]["user_id"] != "u1" {
		t.Error("sync users")
	}
	empty := NewPresenceStore().GetSyncPayload(map[string]any{})
	if len(empty["users"].([]map[string]any)) != 0 {
		t.Error("empty sync users")
	}
}

func TestStoreTakenColors(t *testing.T) {
	s := NewPresenceStore()
	if len(s.TakenColors()) != 0 {
		t.Error("empty taken")
	}
	s.Add("u1", "Alice", "#e74c3c", "admin", "")
	s.Add("u2", "Bob", "#3498db", "viewer", "")
	taken := s.TakenColors()
	if _, ok := taken["#e74c3c"]; !ok {
		t.Error("missing color")
	}
	if _, ok := taken["#3498db"]; !ok {
		t.Error("missing color 2")
	}
	if len(taken) != 2 {
		t.Error("taken count")
	}
}

func TestStorePruneIdle(t *testing.T) {
	withTimeNow(t, 1000.0)
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	s.Add("u2", "Bob", "#000", "viewer", "")
	s.Add("u3", "Carol", "#aaa", "operator", "")
	// Backdate u3 directly (in-package white-box access).
	s.users["u3"].LastActivityAt = 880.0 // 120s ago
	pruned := s.PruneIdle(60.0)
	if !reflect.DeepEqual(pruned, []string{"u3"}) {
		t.Errorf("pruned = %v", pruned)
	}
	if s.Count() != 2 {
		t.Error("count after prune")
	}
	if _, ok := s.Get("u3"); ok {
		t.Error("u3 still present")
	}
	// order must have dropped u3
	all := s.GetAll()
	if len(all) != 2 || all[0].UserID != "u1" || all[1].UserID != "u2" {
		t.Errorf("order after prune: %+v", all)
	}
}

func TestStorePruneIdleNoStale(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	if pruned := s.PruneIdle(60.0); pruned != nil {
		t.Errorf("no stale but pruned %v", pruned)
	}
	if s.Count() != 1 {
		t.Error("count")
	}
}

func TestStoreAddReplaceKeepsOrder(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	s.Add("u2", "Bob", "#000", "viewer", "")
	s.Add("u1", "Alice2", "#111", "admin", "") // replace, keep position
	all := s.GetAll()
	if len(all) != 2 || all[0].UserID != "u1" || all[0].Name != "Alice2" || all[1].UserID != "u2" {
		t.Errorf("replace order: %+v", all)
	}
}

// TestSetPresenceFieldAllKeys exercises every settable field branch.
func TestSetPresenceFieldAllKeys(t *testing.T) {
	s := NewPresenceStore()
	s.Add("u1", "Alice", "#fff", "admin", "")
	fields := map[string]any{
		"scroll_line": 1, "total_lines": 2, "cols": 3, "rows": 4,
		"scroll_range": []int{1, 2}, "selection": map[string]any{"a": 1},
		"pin": map[string]any{"b": 2}, "typing": true, "queued_keys": "q",
		"is_owner": true, "initials": "AL", "name": "New", "color": "#000",
		"role": "op", "user_id": "u1", "last_activity_at": 5.0,
	}
	u, ok, err := s.Update("u1", fields)
	if err != nil || !ok {
		t.Fatalf("update: %v", err)
	}
	if u.ScrollLine != 1 || u.TotalLines != 2 || u.Cols != 3 || u.Rows != 4 ||
		!u.Typing || u.QueuedKeys != "q" || !u.IsOwner || u.Initials != "AL" ||
		u.Name != "New" || u.Color != "#000" || u.Role != "op" {
		t.Errorf("fields not all set: %+v", u)
	}
}

// TestCoercionHelpers covers every numeric/type-coercion branch directly.
func TestCoercionHelpers(t *testing.T) {
	if mustInt(5) != 5 || mustInt(int32(5)) != 5 || mustInt(int64(5)) != 5 ||
		mustInt(5.0) != 5 || mustInt(float32(5)) != 5 || mustInt("x") != 0 {
		t.Error("mustInt")
	}
	if asFloat(1.5) != 1.5 || asFloat(float32(2)) != 2 || asFloat(3) != 3 ||
		asFloat(int64(4)) != 4 || asFloat("x") != 0 {
		t.Error("asFloat")
	}
	if asBool(true) != true || asBool("x") != false {
		t.Error("asBool")
	}
	if asString("s") != "s" || asString(1) != "" {
		t.Error("asString")
	}
	if asDict(map[string]any{"a": 1}) == nil || asDict("x") != nil {
		t.Error("asDict")
	}
	if knownPresenceField("typing") != true || knownPresenceField("bogus") != false {
		t.Error("knownPresenceField")
	}
}

func TestValidatePresenceDictNil(t *testing.T) {
	if err := validatePresenceDict("selection", nil); err != nil {
		t.Errorf("nil should pass: %v", err)
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"reflect"
	"testing"
)

// --- presence_update ---

func TestPresenceUpdateBroadcast(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", nil)
	h.reset()
	if err := d.HandleMessage("w1", ws, map[string]any{
		"type": "presence_update", "scroll_line": 42, "typing": true,
	}, nil); err != nil {
		t.Fatal(err)
	}
	m := h.last().msg
	if m["type"] != "presence_update" || m["scroll_line"] != 42 || m["typing"] != true {
		t.Errorf("broadcast: %v", m)
	}
}

func TestPresenceUpdateAllCursorFields(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	p := fakePrincipal{"sre:alice", "Alice"}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", p)
	h.reset()
	fields := map[string]any{
		"scroll_line": 4, "scroll_range": []any{4, 22}, "total_lines": 9,
		"typing": true, "cols": 132, "rows": 43, "pin": map[string]any{"row": 2, "col": 3},
	}
	msg := map[string]any{"type": "presence_update"}
	for k, v := range fields {
		msg[k] = v
	}
	_ = d.HandleMessage("w1", ws, msg, p)
	m := h.last().msg
	for k, v := range fields {
		if !reflect.DeepEqual(m[k], v) {
			t.Errorf("field %q = %v, want %v", k, m[k], v)
		}
	}
	if u, _ := d.GetPresenceStore("w1").Get("sre:alice"); u.TotalLines != 9 {
		t.Error("total_lines not stored")
	}
}

func TestPresenceUpdateUnknownUserIgnored(t *testing.T) {
	d, h := newService()
	if err := d.HandleMessage("w1", &fakeWS{}, map[string]any{
		"type": "presence_update", "scroll_line": 1,
	}, nil); err != nil {
		t.Fatal(err)
	}
	if h.count() != 0 {
		t.Error("unknown user broadcast")
	}
}

func TestPresenceUpdateResolvesPrincipalUserID(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	p := fakePrincipal{"user-abc", "Alice"}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", p)
	h.reset()
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "presence_update", "scroll_line": 99}, p)
	if h.last().msg["user_id"] != "user-abc" {
		t.Error("principal user_id")
	}
}

func TestPresenceUpdateOversizedSelectionDropped(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	p := fakePrincipal{"sre:alice", "Alice"}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", p)
	h.reset()
	big := map[string]any{}
	for i := 0; i < 17; i++ {
		big[string(rune('a'+i))] = i
	}
	if err := d.HandleMessage("w1", ws, map[string]any{
		"type": "presence_update", "selection": big,
	}, p); err != nil {
		t.Fatal(err)
	}
	if h.count() != 0 {
		t.Error("dropped update must not broadcast")
	}
	if u, _ := d.GetPresenceStore("w1").Get("sre:alice"); u.Selection != nil {
		t.Error("dropped update mutated state")
	}
}

func TestOwnerTypingResetsWarning(t *testing.T) {
	d, _ := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	store := d.GetPresenceStore("w1")
	store.SetOwner(ws.DeckMuxAnonID())
	tm := d.GetTransferManager("w1", nil)
	tm.warningSent = true
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "presence_update", "typing": true}, nil)
	if tm.warningSent {
		t.Error("owner typing must reset warning")
	}
}

func TestNonOwnerTypingDoesNotResetWarning(t *testing.T) {
	d, _ := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "viewer", nil)
	tm := d.GetTransferManager("w1", nil)
	tm.warningSent = true
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "presence_update", "typing": true}, nil)
	if !tm.warningSent {
		t.Error("non-owner typing must not reset warning")
	}
}

func TestPresenceUpdateBroadcastError(t *testing.T) {
	h := &fakeHub{}
	d := NewDeckMuxPresence(h)
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", nil)
	h.errAt = h.n + 1
	if err := d.HandleMessage("w1", ws, map[string]any{"type": "presence_update", "scroll_line": 1}, nil); err == nil {
		t.Error("expected broadcast error")
	}
}

// --- queued_input ---

func TestQueuedInput(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "viewer", nil)
	h.reset()
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "queued_input", "keys": "hello"}, nil)
	m := h.last().msg
	if m["type"] != "presence_update" || m["queued_keys"] != "hello" {
		t.Errorf("queued_input broadcast: %v", m)
	}
}

func TestQueuedInputMissingKeys(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "viewer", nil)
	h.reset()
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "queued_input"}, nil)
	if h.last().msg["queued_keys"] != "" {
		t.Error("missing keys must default empty")
	}
}

func TestQueuedInputUnknownUserIgnored(t *testing.T) {
	d, h := newService()
	_ = d.HandleMessage("w1", &fakeWS{}, map[string]any{"type": "queued_input", "keys": "x"}, nil)
	if h.count() != 0 {
		t.Error("unknown user broadcast")
	}
}

func TestQueuedInputIsolatedPerUser(t *testing.T) {
	d, h := newService()
	wsA, wsB := &fakeWS{}, &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", wsA, "viewer", nil)
	_, _ = d.OnBrowserConnect("w1", wsB, "viewer", nil)
	_ = d.HandleMessage("w1", wsA, map[string]any{"type": "queued_input", "keys": "abc"}, nil)
	h.reset()
	_ = d.HandleMessage("w1", wsB, map[string]any{"type": "queued_input", "keys": "xyz"}, nil)
	if h.last().msg["queued_keys"] != "xyz" {
		t.Error("queues not isolated")
	}
}

func TestQueuedInputBroadcastError(t *testing.T) {
	h := &fakeHub{}
	d := NewDeckMuxPresence(h)
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "viewer", nil)
	h.errAt = h.n + 1
	if err := d.HandleMessage("w1", ws, map[string]any{"type": "queued_input", "keys": "a"}, nil); err == nil {
		t.Error("expected error")
	}
}

// --- control_request ---

func TestControlRequestGrantAndRelease(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	uid := ws.DeckMuxAnonID()
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	h.reset()

	// Grant.
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "control_request"}, nil)
	m := h.last().msg
	if m["type"] != "control_transfer" || m["to_user_id"] != uid || m["from_user_id"] != "" || m["reason"] != "handover" {
		t.Errorf("grant: %v", m)
	}
	if owner, ok := d.GetPresenceStore("w1").GetOwner(); !ok || owner.UserID != uid {
		t.Error("owner not set")
	}
	// Transfer manager keyed by worker id.
	if _, ok := d.transferManagers["w1"]; !ok {
		t.Error("transfer manager missing")
	}

	// Release (already owner).
	h.reset()
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "control_request"}, nil)
	m = h.last().msg
	if m["from_user_id"] != uid || m["to_user_id"] != "" || m["reason"] != "handover" {
		t.Errorf("release: %v", m)
	}
	if _, ok := d.GetPresenceStore("w1").GetOwner(); ok {
		t.Error("owner not cleared")
	}
}

func TestControlRequestIgnoredWhenOtherOwns(t *testing.T) {
	d, h := newService()
	wsA, wsB := &fakeWS{}, &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", wsA, "admin", nil)
	_, _ = d.OnBrowserConnect("w1", wsB, "admin", nil)
	_ = d.HandleMessage("w1", wsA, map[string]any{"type": "control_request"}, nil)
	h.reset()
	_ = d.HandleMessage("w1", wsB, map[string]any{"type": "control_request"}, nil)
	if h.count() != 0 {
		t.Error("must ignore when other owns")
	}
	if owner, _ := d.GetPresenceStore("w1").GetOwner(); owner.UserID != wsA.DeckMuxAnonID() {
		t.Error("owner changed")
	}
}

func TestControlRequestToRealSubject(t *testing.T) {
	d, h := newService()
	pa := fakePrincipal{"sre:alice", "Alice"}
	pb := fakePrincipal{"sre:bob", "Bob"}
	_, _ = d.OnBrowserConnect("w1", &fakeWS{}, "operator", pa)
	wsB := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", wsB, "viewer", pb)
	h.reset()
	_ = d.HandleMessage("w1", wsB, map[string]any{"type": "control_request"}, pb)
	if h.last().msg["to_user_id"] != "sre:bob" {
		t.Error("control to real subject")
	}
}

func TestControlRequestGrantBroadcastError(t *testing.T) {
	h := &fakeHub{}
	d := NewDeckMuxPresence(h)
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	h.errAt = h.n + 1
	if err := d.HandleMessage("w1", ws, map[string]any{"type": "control_request"}, nil); err == nil {
		t.Error("expected grant error")
	}
}

func TestControlRequestReleaseBroadcastError(t *testing.T) {
	h := &fakeHub{}
	d := NewDeckMuxPresence(h)
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "admin", nil)
	_ = d.HandleMessage("w1", ws, map[string]any{"type": "control_request"}, nil) // grant
	h.errAt = h.n + 1
	if err := d.HandleMessage("w1", ws, map[string]any{"type": "control_request"}, nil); err == nil {
		t.Error("expected release error")
	}
}

func TestUnknownMessageTypeIgnored(t *testing.T) {
	d, h := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "operator", nil)
	h.reset()
	if err := d.HandleMessage("w1", ws, map[string]any{"type": "unknown_msg"}, nil); err != nil {
		t.Fatal(err)
	}
	if h.count() != 0 {
		t.Error("unknown type broadcast")
	}
}

// --- cleanup + managers ---

func TestCleanup(t *testing.T) {
	d, _ := newService()
	ws := &fakeWS{}
	_, _ = d.OnBrowserConnect("w1", ws, "viewer", nil)
	d.GetTransferManager("w1", nil)
	if _, ok := d.presenceStores["w1"]; !ok {
		t.Fatal("store missing")
	}
	d.Cleanup("w1")
	if _, ok := d.presenceStores["w1"]; ok {
		t.Error("store not cleaned")
	}
	if _, ok := d.transferManagers["w1"]; ok {
		t.Error("manager not cleaned")
	}
	d.Cleanup("nonexistent") // idempotent, must not panic
}

func TestGetTransferManagerConfig(t *testing.T) {
	d, _ := newService()
	tm := d.GetTransferManager("w1", map[string]any{"auto_transfer_idle_s": 60, "keystroke_queue": "replay"})
	if tm.autoIdleS != 60 || tm.QueueMode() != "replay" {
		t.Errorf("config: %v %q", tm.autoIdleS, tm.QueueMode())
	}
	// Same instance on subsequent calls.
	if d.GetTransferManager("w1", nil) != tm {
		t.Error("not memoized")
	}
	// Defaults for a fresh worker.
	def := d.GetTransferManager("w2", map[string]any{})
	if def.autoIdleS != 30 || def.QueueMode() != "display" {
		t.Error("defaults")
	}
}

func TestGetPresenceStoreMemoized(t *testing.T) {
	d, _ := newService()
	s1 := d.GetPresenceStore("w1")
	if d.GetPresenceStore("w1") != s1 {
		t.Error("store not memoized")
	}
}

func TestReadTransferConfig(t *testing.T) {
	idle, mode := readTransferConfig(nil)
	if idle != 30 || mode != "display" {
		t.Error("nil config defaults")
	}
	idle, mode = readTransferConfig(map[string]any{"auto_transfer_idle_s": 45.0, "keystroke_queue": "replay"})
	if idle != 45 || mode != "replay" {
		t.Error("provided config")
	}
}

// --- AnonConn ---

func TestAnonConn(t *testing.T) {
	a := &AnonConn{}
	first := a.DeckMuxAnonID()
	if first != a.DeckMuxAnonID() {
		t.Error("not stable")
	}
	if len(first) != 32 {
		t.Errorf("id len = %d", len(first))
	}
	b := &AnonConn{}
	if a.DeckMuxAnonID() == b.DeckMuxAnonID() {
		t.Error("distinct connections must get distinct ids")
	}
}

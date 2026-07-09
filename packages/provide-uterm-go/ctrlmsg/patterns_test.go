//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
)

// mustPattern unwraps NewLinkPattern's (LinkPattern, error) result, panicking
// on error so it composes as mustPattern(NewLinkPattern(...)).
func mustPattern(p LinkPattern, err error) LinkPattern {
	if err != nil {
		panic("NewLinkPattern: " + err.Error())
	}
	return p
}

// ---------------------------------------------------------------------------
// LinkPattern construction
// ---------------------------------------------------------------------------

func TestLinkPatternDefaults(t *testing.T) {
	p := mustPattern(NewLinkPattern(`\d+`, "cmd"))
	if p.Pattern != `\d+` || p.Action != "cmd" || p.ID != nil ||
		p.Flags != "g" || p.Group != 0 || p.Payload != "" || p.Hover != "" || p.Class != "" {
		t.Fatalf("unexpected defaults: %+v", p)
	}
}

func TestLinkPatternAllFields(t *testing.T) {
	p := mustPattern(NewLinkPattern(`\((\d{1,5})\)`, "key",
		WithID("sector-nav"), WithFlags("gi"), WithGroup(1),
		WithPayload("$1\r"), WithHover("Warp to sector $1"), WithClass("sector")))
	if p.ID == nil || *p.ID != "sector-nav" || p.Flags != "gi" || p.Group != 1 ||
		p.Payload != "$1\r" || p.Hover != "Warp to sector $1" || p.Class != "sector" {
		t.Fatalf("unexpected fields: %+v", p)
	}
}

func TestLinkPatternInvalidAction(t *testing.T) {
	_, err := NewLinkPattern(`\d+`, "explode")
	if err == nil || !strings.Contains(err.Error(), "invalid action") {
		t.Fatalf("err = %v", err)
	}
	// The error names every valid action.
	for _, a := range []string{"cmd", "url", "key", "focus"} {
		if !strings.Contains(err.Error(), a) {
			t.Errorf("error %q missing %q", err.Error(), a)
		}
	}
}

func TestLinkPatternAllValidActions(t *testing.T) {
	for _, a := range []string{"cmd", "url", "key", "focus"} {
		p := mustPattern(NewLinkPattern("x", a))
		if p.Action != a {
			t.Fatalf("action %q not accepted", a)
		}
	}
}

// ---------------------------------------------------------------------------
// ToFrameEntry
// ---------------------------------------------------------------------------

func TestToFrameEntryMinimal(t *testing.T) {
	p := mustPattern(NewLinkPattern(`\d+`, "url"))
	if !reflect.DeepEqual(p.ToFrameEntry(), map[string]any{"pattern": `\d+`, "action": "url"}) {
		t.Fatalf("entry = %v", p.ToFrameEntry())
	}
}

func TestToFrameEntryOptionalRules(t *testing.T) {
	// id included when set.
	if mustPattern(NewLinkPattern("x", "cmd", WithID("my-id"))).ToFrameEntry()["id"] != "my-id" {
		t.Error("id missing")
	}
	// flags omitted when default "g".
	if _, ok := mustPattern(NewLinkPattern("x", "cmd", WithFlags("g"))).ToFrameEntry()["flags"]; ok {
		t.Error("default flags must be omitted")
	}
	// flags included when non-default.
	if mustPattern(NewLinkPattern("x", "cmd", WithFlags("gi"))).ToFrameEntry()["flags"] != "gi" {
		t.Error("flags gi missing")
	}
	// group omitted when zero, included when nonzero.
	if _, ok := mustPattern(NewLinkPattern("x", "cmd", WithGroup(0))).ToFrameEntry()["group"]; ok {
		t.Error("group 0 must be omitted")
	}
	if mustPattern(NewLinkPattern("x", "cmd", WithGroup(2))).ToFrameEntry()["group"] != 2 {
		t.Error("group 2 missing")
	}
	// payload/hover omitted when empty.
	e := mustPattern(NewLinkPattern("x", "cmd")).ToFrameEntry()
	if _, ok := e["payload"]; ok {
		t.Error("empty payload must be omitted")
	}
	if _, ok := e["hover"]; ok {
		t.Error("empty hover must be omitted")
	}
}

func TestToFrameEntryClassKey(t *testing.T) {
	e := mustPattern(NewLinkPattern("x", "cmd", WithClass("sector"))).ToFrameEntry()
	if e["class"] != "sector" {
		t.Errorf("class = %v", e["class"])
	}
	if _, ok := e["class_"]; ok {
		t.Error("must not emit class_")
	}
	// empty class omitted.
	if _, ok := mustPattern(NewLinkPattern("x", "cmd", WithClass(""))).ToFrameEntry()["class"]; ok {
		t.Error("empty class must be omitted")
	}
}

func TestToFrameEntryFullShape(t *testing.T) {
	p := mustPattern(NewLinkPattern(`\((\d{1,5})\)`, "cmd",
		WithID("sector-nav"), WithFlags("gi"), WithGroup(1),
		WithPayload("$1\r"), WithHover("Warp to sector $1"), WithClass("sector")))
	want := map[string]any{
		"pattern": `\((\d{1,5})\)`, "action": "cmd", "id": "sector-nav", "flags": "gi",
		"group": 1, "payload": "$1\r", "hover": "Warp to sector $1", "class": "sector",
	}
	if !reflect.DeepEqual(p.ToFrameEntry(), want) {
		t.Fatalf("entry = %v", p.ToFrameEntry())
	}
}

// ---------------------------------------------------------------------------
// LinkPatternRegistry
// ---------------------------------------------------------------------------

func TestRegistryEmptySyncPayload(t *testing.T) {
	reg := NewLinkPatternRegistry()
	if !reflect.DeepEqual(reg.SyncPayload(), map[string]any{"type": "link_patterns", "patterns": []any{}}) {
		t.Fatalf("empty payload = %v", reg.SyncPayload())
	}
}

func TestRegistryZeroValueUsable(t *testing.T) {
	// The zero value must work without NewLinkPatternRegistry.
	var reg LinkPatternRegistry
	reg.Register(mustPattern(NewLinkPattern(`\d+`, "cmd", WithID("a"))))
	if len(reg.GetAll()) != 1 {
		t.Fatal("zero-value registry unusable")
	}
}

func TestRegistryRegisterAndGetAll(t *testing.T) {
	reg := NewLinkPatternRegistry()
	p := mustPattern(NewLinkPattern(`\d+`, "cmd", WithID("a")))
	reg.Register(p)
	if !reflect.DeepEqual(reg.GetAll(), []LinkPattern{p}) {
		t.Fatalf("get_all = %v", reg.GetAll())
	}
	payload := reg.SyncPayload()
	if payload["type"] != "link_patterns" {
		t.Fatalf("type = %v", payload["type"])
	}
	if !reflect.DeepEqual(payload["patterns"], []any{p.ToFrameEntry()}) {
		t.Fatalf("patterns = %v", payload["patterns"])
	}
}

func TestRegistryInsertionOrder(t *testing.T) {
	reg := NewLinkPatternRegistry()
	a := mustPattern(NewLinkPattern("a", "cmd", WithID("a")))
	b := mustPattern(NewLinkPattern("b", "url", WithID("b")))
	c := mustPattern(NewLinkPattern("c", "key", WithID("c")))
	reg.Register(a)
	reg.Register(b)
	reg.Register(c)
	if !reflect.DeepEqual(reg.GetAll(), []LinkPattern{a, b, c}) {
		t.Fatalf("order = %v", reg.GetAll())
	}
	patterns := reg.SyncPayload()["patterns"].([]any)
	for i, want := range []string{"a", "b", "c"} {
		if patterns[i].(map[string]any)["pattern"] != want {
			t.Fatalf("payload order wrong at %d", i)
		}
	}
}

func TestRegistryUnregister(t *testing.T) {
	reg := NewLinkPatternRegistry()
	reg.Register(mustPattern(NewLinkPattern("x", "cmd", WithID("x"))))
	if !reg.Unregister("x") {
		t.Error("unregister existing must return true")
	}
	if len(reg.GetAll()) != 0 {
		t.Error("must be empty after unregister")
	}
	if reg.Unregister("nonexistent") {
		t.Error("unregister missing must return false")
	}
}

func TestRegistryClear(t *testing.T) {
	reg := NewLinkPatternRegistry()
	for i := 0; i < 3; i++ {
		reg.Register(mustPattern(NewLinkPattern(fmt.Sprint(i), "cmd", WithID(fmt.Sprint(i)))))
	}
	reg.Clear()
	if len(reg.GetAll()) != 0 {
		t.Error("clear must empty get_all")
	}
	if !reflect.DeepEqual(reg.SyncPayload(), map[string]any{"type": "link_patterns", "patterns": []any{}}) {
		t.Fatalf("clear payload = %v", reg.SyncPayload())
	}
}

func TestRegistrySyncPayloadNonDestructiveAndFresh(t *testing.T) {
	reg := NewLinkPatternRegistry()
	p := mustPattern(NewLinkPattern("x", "cmd", WithID("x")))
	reg.Register(p)
	a := reg.SyncPayload()
	if !reflect.DeepEqual(reg.GetAll(), []LinkPattern{p}) {
		t.Error("sync_payload must be non-destructive")
	}
	b := reg.SyncPayload()
	if !reflect.DeepEqual(a, b) {
		t.Error("payloads must be equal")
	}
	a["patterns"] = "mutated"
	if reflect.DeepEqual(a["patterns"], b["patterns"]) {
		t.Error("sync_payload must return a fresh map each call")
	}
}

func TestRegistryIDLessPatterns(t *testing.T) {
	reg := NewLinkPatternRegistry()
	p1 := mustPattern(NewLinkPattern("a", "cmd"))
	p2 := mustPattern(NewLinkPattern("b", "cmd"))
	reg.Register(p1)
	reg.Register(p2)
	if !reflect.DeepEqual(reg.GetAll(), []LinkPattern{p1, p2}) {
		t.Fatalf("id-less get_all = %v", reg.GetAll())
	}
	// A string id that was never registered returns false; the counter key
	// (int 0) never collides with the string "0".
	if reg.Unregister("0") {
		t.Error("string id must not match int counter key")
	}
	if len(reg.GetAll()) != 2 {
		t.Fatalf("expected 2 id-less patterns, got %d", len(reg.GetAll()))
	}
}

func TestRegistrySameIDReplace(t *testing.T) {
	reg := NewLinkPatternRegistry()
	p1 := mustPattern(NewLinkPattern("first", "cmd", WithID("shared")))
	p2 := mustPattern(NewLinkPattern("second", "url", WithID("shared")))
	reg.Register(p1)
	reg.Register(p2)
	all := reg.GetAll()
	if len(all) != 1 || all[0].Pattern != "second" {
		t.Fatalf("replace failed: %v", all)
	}
}

func TestRegistrySameIDReplacePreservesOrder(t *testing.T) {
	reg := NewLinkPatternRegistry()
	reg.Register(mustPattern(NewLinkPattern("a", "cmd", WithID("a"))))
	reg.Register(mustPattern(NewLinkPattern("b", "cmd", WithID("b"))))
	reg.Register(mustPattern(NewLinkPattern("c", "cmd", WithID("c"))))
	reg.Register(mustPattern(NewLinkPattern("b-updated", "url", WithID("b"))))
	var got []string
	for _, p := range reg.GetAll() {
		got = append(got, p.Pattern)
	}
	if !reflect.DeepEqual(got, []string{"a", "b-updated", "c"}) {
		t.Fatalf("order after replace = %v", got)
	}
}

func TestRegistryClearResetsCounter(t *testing.T) {
	reg := NewLinkPatternRegistry()
	reg.Register(mustPattern(NewLinkPattern("a", "cmd")))
	reg.Clear()
	reg.Register(mustPattern(NewLinkPattern("b", "cmd")))
	if len(reg.GetAll()) != 1 {
		t.Fatalf("counter not reset: %d entries", len(reg.GetAll()))
	}
}

// ---------------------------------------------------------------------------
// Concurrency smoke test (run with -race)
// ---------------------------------------------------------------------------

func TestRegistryConcurrentMixedOperations(t *testing.T) {
	reg := NewLinkPatternRegistry()
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			reg.Register(mustPattern(NewLinkPattern(fmt.Sprint(i), "cmd", WithID(fmt.Sprintf("p%d", i)))))
		}(i)
	}
	wg.Wait()

	for i := 0; i < 10; i += 2 {
		wg.Add(1)
		go func(i int) { defer wg.Done(); reg.Unregister(fmt.Sprintf("p%d", i)) }(i)
	}
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _ = reg.SyncPayload() }()
	}
	wg.Wait()

	final := reg.SyncPayload()
	if final["type"] != "link_patterns" {
		t.Fatalf("final type = %v", final["type"])
	}
	if _, ok := final["patterns"].([]any); !ok {
		t.Fatal("patterns is not a list")
	}
}

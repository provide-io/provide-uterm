//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package capturestate

import (
	"errors"
	"os"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// Setting the owner or visibility of a previous session. Port of
// CaptureStateStore.update_previous and its tests.

func ptr(s string) *string { return &s }

func TestAnUpdateRewritesTheRecordKeepingWhatItDoesNotChange(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "last screen")
	before := readJSON(t, previousFile(s, sid))
	got, err := s.UpdatePrevious(sid, ptr("blackbetty:7"), ptr("public"))
	if err != nil || got == nil {
		t.Fatalf("update: %v %v", got, err)
	}
	if got.Screen != nil || got.Summary().Owner == nil || *got.Summary().Owner != "blackbetty:7" ||
		got.Definition.Visibility != "public" || *got.EndedAt != *s.PreviousSessions()[0].EndedAt {
		t.Fatalf("returned %+v", got)
	}
	after := readJSON(t, previousFile(s, sid))
	session := after["session"].(map[string]any)
	if session["owner"] != "blackbetty:7" || session["visibility"] != "public" {
		t.Fatalf("session %v", session)
	}
	// Whatever else the file held is as it was, the screen and when it ended above all.
	for _, key := range []string{"screen", "scrollback", "ended_at", "saved_at", "login", "recording_instance"} {
		if !jsonEqual(before[key], after[key]) {
			t.Errorf("%s changed: %v -> %v", key, before[key], after[key])
		}
	}
	if mode(t, previousFile(s, sid)) != 0o600 {
		t.Error("the rewritten file is not owner-only")
	}
}

func jsonEqual(a, b any) bool { return reflect.DeepEqual(a, b) }

func TestOneFieldLeavesTheOtherAsItWas(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "x")
	if _, err := s.UpdatePrevious(sid, nil, ptr("operator")); err != nil {
		t.Fatal(err)
	}
	kept, _ := s.PreviousSummary(sid)
	if *kept.Definition.Owner != "alice" || kept.Definition.Visibility != "operator" {
		t.Fatalf("after visibility: %+v", kept.Definition)
	}
	if _, err := s.UpdatePrevious(sid, ptr("bob"), nil); err != nil {
		t.Fatal(err)
	}
	kept, _ = s.PreviousSummary(sid)
	if *kept.Definition.Owner != "bob" || kept.Definition.Visibility != "operator" {
		t.Fatalf("after owner: %+v", kept.Definition)
	}
}

func TestAnUpdateSurvivesARestart(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "screen kept")
	if _, err := s.UpdatePrevious(sid, ptr("blackbetty:7"), ptr("public")); err != nil {
		t.Fatal(err)
	}
	again := New(s.dir, true, 7, 10, s.logger)
	again.now = s.now
	again.LoadPrevious()
	kept, ok := again.PreviousSummary(sid)
	if !ok || *kept.Definition.Owner != "blackbetty:7" || kept.Definition.Visibility != "public" {
		t.Fatalf("reloaded %+v", kept)
	}
	full, ok := again.PreviousSession(sid)
	if !ok || *full.Screen != "screen kept" {
		t.Fatalf("screen %+v", full)
	}
}

func TestAnIdNeverListedIsNone(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "x")
	got, err := s.UpdatePrevious("pam-alice-capture-9-9", ptr("x"), nil)
	if got != nil || err != nil || len(s.PreviousSessions()) != 1 {
		t.Fatalf("got %v %v", got, err)
	}
}

func TestAFileGoneOrUnreadableSinceListingIsNoneAndUnlisted(t *testing.T) {
	for name, damage := range map[string]func(path string){
		"gone":    func(path string) { _ = os.Remove(path) },
		"garbled": func(path string) { _ = os.WriteFile(path, []byte("{not json"), 0o600) },
	} {
		s, c, _ := newStore(t, true, 7, 10)
		endedAfter(t, s, c, sid, "alice", "x")
		damage(previousFile(s, sid))
		got, err := s.UpdatePrevious(sid, ptr("bob"), nil)
		if got != nil || err != nil {
			t.Errorf("%s: got %v %v", name, got, err)
		}
		if _, ok := s.PreviousSummary(sid); ok {
			t.Errorf("%s: still listed", name)
		}
		// Nothing is written in its place, and an unreadable file is not removed.
		if exists(previousFile(s, sid)) != (name == "garbled") {
			t.Errorf("%s: file exists = %v", name, exists(previousFile(s, sid)))
		}
	}
}

func TestAWriteThatFailsLeavesTheFileAndTheListingAsTheyWere(t *testing.T) {
	s, c, logs := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "x")
	before := readJSON(t, previousFile(s, sid))
	if err := os.Chmod(s.previousDir, 0o500); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = os.Chmod(s.previousDir, 0o700) }()
	got, err := s.UpdatePrevious(sid, ptr("bob"), nil)
	if got != nil || !errors.Is(err, ErrNotSaved) {
		t.Fatalf("got %v %v", got, err)
	}
	_ = os.Chmod(s.previousDir, 0o700)
	if !jsonEqual(before, readJSON(t, previousFile(s, sid))) {
		t.Error("the file changed")
	}
	kept, _ := s.PreviousSummary(sid)
	if *kept.Definition.Owner != "alice" || !strings.Contains(logs.String(), "capture_state_write_failed") {
		t.Fatalf("listing %+v, logs %s", kept.Definition, logs.String())
	}
}

func TestTheNewOwnersCapDropsItsOldest(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	for pid := 0; pid < PreviousPerOwner; pid++ {
		endedAfter(t, s, c, "pam-u-capture-"+strconv.Itoa(pid)+"-9", "blackbetty:7", "x")
	}
	// The newest of all, so it is the one the cap keeps.
	endedAfter(t, s, c, sid, "alice", "x")
	got, err := s.UpdatePrevious(sid, ptr("blackbetty:7"), nil)
	if err != nil || got == nil {
		t.Fatalf("update: %v %v", got, err)
	}
	if exists(previousFile(s, "pam-u-capture-0-9")) || !exists(previousFile(s, "pam-u-capture-1-9")) {
		t.Error("the oldest of the new owner's was not the one dropped")
	}
	if len(s.PreviousSessions()) != PreviousPerOwner {
		t.Fatalf("new owner keeps %d", len(s.PreviousSessions()))
	}
}

func TestARecordTheNewOwnersCapDropsIsNoneAndItsFileIsGone(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	// Ended before the twenty, so the new owner's cap finds it the oldest.
	endedAfter(t, s, c, sid, "alice", "x")
	for pid := 0; pid < PreviousPerOwner; pid++ {
		endedAfter(t, s, c, "pam-u-capture-"+strconv.Itoa(pid)+"-9", "blackbetty:7", "x")
	}
	got, err := s.UpdatePrevious(sid, ptr("blackbetty:7"), nil)
	if got != nil || err != nil {
		t.Fatalf("got %v %v", got, err)
	}
	if _, ok := s.PreviousSummary(sid); ok || exists(previousFile(s, sid)) {
		t.Error("the evicted record stayed")
	}
	if len(s.PreviousSessions()) != PreviousPerOwner {
		t.Errorf("%d kept", len(s.PreviousSessions()))
	}
}

// An update takes the session's own lock, as every other write of its files
// does, so two writes never interleave.
func TestAnUpdateWaitsForTheSessionsLock(t *testing.T) {
	s, c, _ := newStore(t, true, 7, 10)
	endedAfter(t, s, c, sid, "alice", "x")
	w := s.writer(sid)
	w.Lock()
	var wg sync.WaitGroup
	wg.Add(1)
	done := make(chan struct{})
	go func() {
		defer wg.Done()
		_, _ = s.UpdatePrevious(sid, ptr("bob"), nil)
		close(done)
	}()
	select {
	case <-done:
		t.Fatal("the update did not wait")
	case <-time.After(50 * time.Millisecond):
	}
	w.Unlock()
	wg.Wait()
	if kept, _ := s.PreviousSummary(sid); *kept.Definition.Owner != "bob" {
		t.Fatalf("owner %v", *kept.Definition.Owner)
	}
}

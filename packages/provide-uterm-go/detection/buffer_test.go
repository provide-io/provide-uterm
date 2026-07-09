//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import "testing"

// pinNow fixes the buffer clock for a test and restores it afterward.
func pinNow(t *testing.T, now float64) {
	t.Helper()
	restore := nowSeconds
	nowSeconds = func() float64 { return now }
	t.Cleanup(func() { nowSeconds = restore })
}

func bufSnap(screen, hash string, capturedAt float64) Snapshot {
	return Snapshot{"screen": screen, "screen_hash": hash, "captured_at": capturedAt}
}

func TestScreenBufferCreation(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	sb := mgr.AddScreen(bufSnap("hi", "h1", 1.0))
	if sb.Screen != "hi" || sb.ScreenHash != "h1" || sb.CapturedAt != 1.0 {
		t.Errorf("sb = %+v", sb)
	}
	if sb.MatchedPromptID != "" {
		t.Error("matched prompt id default empty")
	}
}

func TestAddAndGetRecent(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	for i := 0; i < 3; i++ {
		mgr.AddScreen(bufSnap("s", "h"+string(rune('0'+i)), float64(i)))
	}
	if got := mgr.GetRecent(2); len(got) != 2 {
		t.Errorf("recent = %d", len(got))
	}
	if got := mgr.GetRecent(3); len(got) != 3 {
		t.Errorf("n == len = %d", len(got))
	}
	if got := mgr.GetRecent(10); len(got) != 3 {
		t.Errorf("n > len = %d", len(got))
	}
}

func TestMaxSizeOverflow(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(3)
	for i := 0; i < 5; i++ {
		mgr.AddScreen(bufSnap("s", "h"+string(rune('0'+i)), float64(i)))
	}
	if mgr.Len() != 3 {
		t.Errorf("len = %d", mgr.Len())
	}
	// oldest evicted: first remaining is h2
	if got := mgr.GetRecent(10); got[0].ScreenHash != "h2" {
		t.Errorf("first = %q", got[0].ScreenHash)
	}
	if mgr.MaxSize() != 3 {
		t.Error("max size")
	}
}

func TestDetectIdle(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	mgr.AddScreen(bufSnap("s", "same", 95.0))
	mgr.AddScreen(bufSnap("s", "same", 100.0))
	if !mgr.DetectIdleState(2.0) {
		t.Error("stable screen should be idle")
	}
	mgr2 := NewBufferManager(5)
	mgr2.AddScreen(bufSnap("s1", "h1", 99.0))
	mgr2.AddScreen(bufSnap("s2", "h2", 100.0))
	if mgr2.DetectIdleState(2.0) {
		t.Error("changing screen should not be idle")
	}
}

func TestDetectIdleFreshManager(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	if mgr.DetectIdleState(0.0) {
		t.Error("fresh manager never idle")
	}
}

func TestClear(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	mgr.AddScreen(bufSnap("s", "h", 100.0))
	mgr.Clear()
	if mgr.Len() != 0 || len(mgr.GetRecent(5)) != 0 {
		t.Error("clear")
	}
	if mgr.DetectIdleState(0.0) {
		t.Error("cleared manager not idle")
	}
}

func TestTimeSinceChangeTracking(t *testing.T) {
	pinNow(t, 100.0)
	mgr := NewBufferManager(5)
	b1 := mgr.AddScreen(bufSnap("s", "h1", 10.0))
	if b1.TimeSinceLastChange != 0.0 {
		t.Errorf("first change = %f", b1.TimeSinceLastChange)
	}
	// same hash later: time since last change grows
	b2 := mgr.AddScreen(bufSnap("s", "h1", 15.0))
	if b2.TimeSinceLastChange != 5.0 {
		t.Errorf("unchanged = %f", b2.TimeSinceLastChange)
	}
	// new hash: measured from previous change time
	b3 := mgr.AddScreen(bufSnap("s2", "h2", 18.0))
	if b3.TimeSinceLastChange != 8.0 {
		t.Errorf("changed = %f", b3.TimeSinceLastChange)
	}
}

func TestAddScreenMissingCapturedAtUsesNow(t *testing.T) {
	pinNow(t, 500.0)
	mgr := NewBufferManager(5)
	b := mgr.AddScreen(Snapshot{"screen": "s", "screen_hash": "h"})
	if b.CapturedAt != 500.0 {
		t.Errorf("captured_at = %f", b.CapturedAt)
	}
	// non-numeric captured_at also falls back to now
	b2 := mgr.AddScreen(Snapshot{"screen": "s", "screen_hash": "h2", "captured_at": "bogus"})
	if b2.CapturedAt != 500.0 {
		t.Errorf("bogus captured_at = %f", b2.CapturedAt)
	}
}

func TestToFloat(t *testing.T) {
	if f, ok := toFloat(1.5); !ok || f != 1.5 {
		t.Error("float64")
	}
	if f, ok := toFloat(3); !ok || f != 3.0 {
		t.Error("int")
	}
	if f, ok := toFloat(int64(4)); !ok || f != 4.0 {
		t.Error("int64")
	}
	if _, ok := toFloat("x"); ok {
		t.Error("string")
	}
}

func TestNowSecondsRealClock(t *testing.T) {
	// exercise the real clock function (not pinned)
	if nowSeconds() <= 0 {
		t.Error("real clock")
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func sampleSnapshot() Snapshot {
	return Snapshot{
		"screen":      "Test screen content\nLine 2\nLine 3",
		"screen_hash": "abc123def456", // pragma: allowlist secret
		"captured_at": 1700000000.0,
		"cursor":      map[string]any{"x": 10, "y": 5},
		"cols":        80,
		"rows":        25,
		"term":        "ANSI",
	}
}

func TestSaverInitialization(t *testing.T) {
	dir := t.TempDir()
	saver := NewScreenSaver(dir, "tradewars", true)
	if !saver.Enabled() || saver.Namespace() != "tradewars" || saver.GetSavedCount() != 0 {
		t.Error("init state")
	}
	noNS := NewScreenSaver(dir, "", true)
	if noNS.Namespace() != "" {
		t.Error("no namespace")
	}
}

func TestSaverConfiguration(t *testing.T) {
	dir := t.TempDir()
	saver := NewScreenSaver(dir, "tradewars", true)
	saver.SetEnabled(false)
	if saver.Enabled() {
		t.Error("disable")
	}
	saver.SetEnabled(true)
	saver.SetNamespace("other_game")
	if saver.Namespace() != "other_game" {
		t.Error("namespace")
	}
	if saver.GetScreensDir() != filepath.Join(dir, "games", "other_game", "screens") {
		t.Error("screens dir with ns")
	}
	saver.SetNamespace("")
	if saver.GetScreensDir() != filepath.Join(dir, "shared", "screens") {
		t.Error("shared screens dir")
	}
}

func TestSaveScreenBasic(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	path, err := saver.SaveScreen(sampleSnapshot(), "", false)
	if err != nil || path == "" {
		t.Fatalf("save: %q %v", path, err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("file missing")
	}
	if filepath.Dir(path) != saver.GetScreensDir() {
		t.Error("parent dir")
	}
	if saver.GetSavedCount() != 1 {
		t.Error("count")
	}
	if !strings.HasSuffix(path, ".txt") {
		t.Error("suffix")
	}
}

func TestSaveScreenWithPromptID(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	path, err := saver.SaveScreen(sampleSnapshot(), "prompt.warp", false)
	if err != nil || !strings.Contains(filepath.Base(path), "prompt.warp") {
		t.Errorf("path = %q err = %v", path, err)
	}
	content, _ := os.ReadFile(path)
	if !strings.Contains(string(content), "Prompt ID: prompt.warp") {
		t.Error("prompt id in content")
	}
}

func TestSaveScreenDisabled(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", false)
	path, err := saver.SaveScreen(sampleSnapshot(), "", false)
	if err != nil || path != "" || saver.GetSavedCount() != 0 {
		t.Error("disabled saver must not save")
	}
}

func TestSaveScreenNoDuplicates(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	p1, _ := saver.SaveScreen(sampleSnapshot(), "", false)
	p2, _ := saver.SaveScreen(sampleSnapshot(), "", false)
	if p1 == "" || p2 != "" || saver.GetSavedCount() != 1 {
		t.Error("dup skipped")
	}
}

func TestSaveScreenForceDuplicate(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	p1, _ := saver.SaveScreen(sampleSnapshot(), "", false)
	p2, err := saver.SaveScreen(sampleSnapshot(), "", true)
	if err != nil || p1 == "" || p2 == "" || p1 == p2 {
		t.Errorf("force dup: %q vs %q (%v)", p1, p2, err)
	}
	if !strings.Contains(filepath.Base(p2), "-dup1") {
		t.Errorf("dup suffix: %q", p2)
	}
	if saver.GetSavedCount() != 1 {
		t.Error("count stays 1")
	}
}

func TestSaveScreenMissingData(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	for _, s := range []Snapshot{
		{"screen_hash": "abc123"},
		{"screen": "content"},
		{"screen": "", "screen_hash": "abc123"},
	} {
		if path, err := saver.SaveScreen(s, "", false); err != nil || path != "" {
			t.Errorf("missing data saved: %v", s)
		}
	}
}

func TestSaveScreenFileContent(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	path, _ := saver.SaveScreen(sampleSnapshot(), "", false)
	content, _ := os.ReadFile(path)
	text := string(content)
	for _, want := range []string{
		"SCREEN CAPTURE", "Hash: abc123def456", "Cursor: (10, 5)",
		"Size: 80x25", "Terminal: ANSI", "Test screen content",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("content missing %q", want)
		}
	}
}

func TestSaveScreenMetadataVariants(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	snapshot := Snapshot{
		"screen": "content", "screen_hash": "hash1", "captured_at": 1700000000.0,
		"prompt_detected":        map[string]any{"input_type": "single_key", "is_idle": true},
		"cursor_at_end":          true,
		"time_since_last_change": 2.5,
	}
	path, _ := saver.SaveScreen(snapshot, "prompt.command", false)
	content, _ := os.ReadFile(path)
	text := string(content)
	for _, want := range []string{
		"Input Type: single_key", "Idle: True",
		"Cursor at End: True", "Time Since Last Change: 2.50s",
		"Cursor: (0, 0)",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("metadata missing %q", want)
		}
	}
	// defaults: no cursor, no prompt_detected, missing input_type
	saver2 := NewScreenSaver(t.TempDir(), "", true)
	path2, _ := saver2.SaveScreen(Snapshot{
		"screen": "c", "screen_hash": "h2", "captured_at": 1700000000.0,
		"prompt_detected": map[string]any{},
		"cursor_at_end":   false,
	}, "", false)
	content2, _ := os.ReadFile(path2)
	if !strings.Contains(string(content2), "Input Type: unknown") ||
		!strings.Contains(string(content2), "Idle: False") ||
		!strings.Contains(string(content2), "Cursor at End: False") {
		t.Errorf("defaults content:\n%s", content2)
	}
}

func TestClearSavedHashesAllowsResave(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	if p, _ := saver.SaveScreen(sampleSnapshot(), "", false); p == "" {
		t.Fatal("first save")
	}
	if p, _ := saver.SaveScreen(sampleSnapshot(), "", false); p != "" {
		t.Fatal("dup skipped")
	}
	saver.ClearSavedHashes()
	if saver.GetSavedCount() != 0 {
		t.Error("count cleared")
	}
	// The base file still exists, so re-save (force=false) overwrites in place.
	if p, _ := saver.SaveScreen(sampleSnapshot(), "", false); p == "" {
		t.Error("resave after clear")
	}
}

func TestMultipleUniqueScreens(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	for i := 0; i < 5; i++ {
		s := Snapshot{"screen": "Screen", "screen_hash": strings.Repeat("h", i+1), "captured_at": 1700000000.0 + float64(i)}
		if p, err := saver.SaveScreen(s, "", false); err != nil || p == "" {
			t.Fatalf("save %d: %v", i, err)
		}
	}
	if saver.GetSavedCount() != 5 {
		t.Errorf("count = %d", saver.GetSavedCount())
	}
}

func TestWriteFailureDoesNotAddHash(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tradewars", true)
	saver.writeFile = func(string, string) error { return errors.New("disk full") }
	if _, err := saver.SaveScreen(sampleSnapshot(), "", false); err == nil {
		t.Fatal("expected write error")
	}
	if saver.GetSavedCount() != 0 {
		t.Error("hash must not be recorded on failure")
	}
	// retry succeeds after restoring the writer
	saver.writeFile = func(path, content string) error { return os.WriteFile(path, []byte(content), 0o644) }
	if p, err := saver.SaveScreen(sampleSnapshot(), "", false); err != nil || p == "" {
		t.Error("retry should succeed")
	}
	if saver.GetSavedCount() != 1 {
		t.Error("count after retry")
	}
}

func TestDupFallbackExhaustionRaises(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "tw", true)
	p1, err := saver.SaveScreen(sampleSnapshot(), "", false)
	if err != nil || p1 == "" {
		t.Fatal("base save")
	}
	stem := strings.TrimSuffix(filepath.Base(p1), ".txt")
	dir := saver.GetScreensDir()
	for i := 1; i < 10000; i++ {
		f, cerr := os.Create(filepath.Join(dir, stem+"-dup"+strconv.Itoa(i)+".txt"))
		if cerr != nil {
			t.Fatal(cerr)
		}
		_ = f.Close()
	}
	if _, err := saver.SaveScreen(sampleSnapshot(), "", true); err == nil || !strings.Contains(err.Error(), "10,000") {
		t.Errorf("exhaustion err = %v", err)
	}
}

func TestSaveScreenMkdirFailure(t *testing.T) {
	dir := t.TempDir()
	// Create a FILE where the "games" directory should be, so MkdirAll fails.
	if err := os.WriteFile(filepath.Join(dir, "games"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	saver := NewScreenSaver(dir, "tw", true)
	if _, err := saver.SaveScreen(sampleSnapshot(), "", false); err == nil {
		t.Error("expected mkdir failure")
	}
}

func TestSaveScreenShortHash(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "", true)
	// hash shorter than 8 chars is used verbatim
	p, err := saver.SaveScreen(Snapshot{"screen": "c", "screen_hash": "abc", "captured_at": 1700000000.0}, "", false)
	if err != nil || !strings.Contains(filepath.Base(p), "-abc.") {
		t.Errorf("short hash path = %q err=%v", p, err)
	}
}

func TestSaveScreenMissingCapturedAt(t *testing.T) {
	saver := NewScreenSaver(t.TempDir(), "", true)
	p, err := saver.SaveScreen(Snapshot{"screen": "c", "screen_hash": "deadbeef99"}, "", false)
	if err != nil || p == "" {
		t.Errorf("missing captured_at: %q %v", p, err)
	}
}

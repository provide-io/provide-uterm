//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ScreenSaver persists unique screens to disk in an organized directory tree.
// Faithful port of the Python ScreenSaver.
type ScreenSaver struct {
	baseDir     string
	namespace   string // "" means no namespace (Python None)
	enabled     bool
	savedHashes map[string]struct{}
	writeFile   func(path, content string) error // injectable for tests
}

// NewScreenSaver constructs a screen saver rooted at baseDir. An empty
// namespace routes screens under "shared/".
func NewScreenSaver(baseDir, namespace string, enabled bool) *ScreenSaver {
	return &ScreenSaver{
		baseDir:     baseDir,
		namespace:   namespace,
		enabled:     enabled,
		savedHashes: map[string]struct{}{},
		writeFile: func(path, content string) error {
			return os.WriteFile(path, []byte(content), 0o644)
		},
	}
}

// SetEnabled enables or disables screen saving.
func (s *ScreenSaver) SetEnabled(enabled bool) { s.enabled = enabled }

// SetNamespace sets the namespace for screen organization.
func (s *ScreenSaver) SetNamespace(namespace string) { s.namespace = namespace }

// Enabled reports whether saving is enabled.
func (s *ScreenSaver) Enabled() bool { return s.enabled }

// Namespace returns the current namespace ("" when unset).
func (s *ScreenSaver) Namespace() string { return s.namespace }

// GetScreensDir returns the directory screens are written to.
func (s *ScreenSaver) GetScreensDir() string {
	if s.namespace != "" {
		return filepath.Join(s.baseDir, "games", s.namespace, "screens")
	}
	return filepath.Join(s.baseDir, "shared", "screens")
}

// GetSavedCount returns the number of unique screen hashes saved.
func (s *ScreenSaver) GetSavedCount() int { return len(s.savedHashes) }

// ClearSavedHashes clears the set of saved screen hashes.
func (s *ScreenSaver) ClearSavedHashes() {
	s.savedHashes = map[string]struct{}{}
}

// SaveScreen writes a screen snapshot to disk, returning the written path
// ("" when nothing was saved). It skips already-saved hashes unless force is
// set.
func (s *ScreenSaver) SaveScreen(snapshot Snapshot, promptID string, force bool) (string, error) {
	if !s.enabled {
		return "", nil
	}
	screen, _ := snapshot["screen"].(string)
	screenHash, _ := snapshot["screen_hash"].(string)
	capturedAt := nowSeconds()
	if v, ok := snapshot["captured_at"]; ok {
		if f, fok := toFloat(v); fok {
			capturedAt = f
		}
	}
	if screen == "" || screenHash == "" {
		return "", nil
	}
	if _, saved := s.savedHashes[screenHash]; !force && saved {
		return "", nil
	}

	screensDir := s.GetScreensDir()
	if err := os.MkdirAll(screensDir, 0o755); err != nil {
		return "", err
	}

	sec := int64(capturedAt)
	nsec := int64((capturedAt - float64(sec)) * 1e9)
	timestamp := time.Unix(sec, nsec).Format("20060102-150405")
	hashShort := screenHash
	if len(hashShort) > 8 {
		hashShort = hashShort[:8]
	}
	promptSuffix := ""
	if promptID != "" {
		promptSuffix = "-" + promptID
	}
	filename := timestamp + "-" + hashShort + promptSuffix + ".txt"

	screenFile, err := resolveFilePath(screensDir, filename, force)
	if err != nil {
		return "", err
	}

	content := s.formatScreenFile(snapshot, promptID, capturedAt)
	if werr := s.writeFile(screenFile, content); werr != nil {
		return "", werr
	}
	s.savedHashes[screenHash] = struct{}{}
	return screenFile, nil
}

// resolveFilePath determines the final write path, appending a -dupN suffix on
// forced saves that would overwrite an existing file.
func resolveFilePath(screensDir, filename string, force bool) (string, error) {
	screenFile := filepath.Join(screensDir, filename)
	if force && fileExists(screenFile) {
		stem := strings.TrimSuffix(filename, filepath.Ext(filename))
		for i := 1; i < 10000; i++ {
			candidate := filepath.Join(screensDir, fmt.Sprintf("%s-dup%d.txt", stem, i))
			if !fileExists(candidate) {
				return candidate, nil
			}
		}
		//nolint:staticcheck // ST1005: message kept byte-identical to the Python implementation
		return "", fmt.Errorf("Could not find free filename after 10,000 attempts for %s", filename)
	}
	return screenFile, nil
}

func (s *ScreenSaver) formatScreenFile(snapshot Snapshot, promptID string, capturedAt float64) string {
	bar := strings.Repeat("=", 80)
	cx, cy := 0, 0
	if cursor, ok := snapshot["cursor"].(map[string]any); ok {
		cx = pyIntOr0(cursor["x"])
		cy = pyIntOr0(cursor["y"])
	}
	sec := int64(capturedAt)
	nsec := int64((capturedAt - float64(sec)) * 1e9)
	ts := time.Unix(sec, nsec).Format("2006-01-02 15:04:05")

	lines := []string{
		bar,
		"SCREEN CAPTURE",
		bar,
		"Timestamp: " + ts,
		"Hash: " + snapshotStr(snapshot, "screen_hash", "unknown"),
		fmt.Sprintf("Cursor: (%d, %d)", cx, cy),
		fmt.Sprintf("Size: %dx%d", snapshotInt(snapshot, "cols", 80), snapshotInt(snapshot, "rows", 25)),
		"Terminal: " + snapshotStr(snapshot, "term", "ANSI"),
	}
	if promptID != "" {
		lines = append(lines, "Prompt ID: "+promptID)
	}
	if detected, ok := snapshot["prompt_detected"].(map[string]any); ok {
		lines = append(lines,
			"Input Type: "+strOr(detected["input_type"], "unknown"),
			"Idle: "+pyBoolStr(detected["is_idle"]),
		)
	}
	if v, ok := snapshot["cursor_at_end"]; ok && v != nil {
		lines = append(lines, "Cursor at End: "+pyBoolStr(v))
	}
	if v, ok := snapshot["time_since_last_change"]; ok && v != nil {
		if f, fok := toFloat(v); fok {
			lines = append(lines, fmt.Sprintf("Time Since Last Change: %.2fs", f))
		}
	}
	lines = append(lines, bar, "", snapshotStr(snapshot, "screen", ""))
	return strings.Join(lines, "\n")
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func snapshotStr(snapshot Snapshot, key, def string) string {
	if v, ok := snapshot[key]; ok {
		if s, sok := v.(string); sok {
			return s
		}
	}
	return def
}

func snapshotInt(snapshot Snapshot, key string, def int) int {
	if v, ok := snapshot[key]; ok {
		if n, nok := toFloat(v); nok {
			return int(n)
		}
	}
	return def
}

func strOr(v any, def string) string {
	if s, ok := v.(string); ok {
		return s
	}
	return def
}

// pyBoolStr renders a bool the way Python str(bool) would ("True"/"False").
func pyBoolStr(v any) string {
	if pyTruthy(v) {
		return "True"
	}
	return "False"
}

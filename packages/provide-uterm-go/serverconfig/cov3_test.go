//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSessionExplicitConnectorConfig(t *testing.T) {
	c := mustConfig(t, map[string]any{"sessions": []any{map[string]any{
		"session_id":       "s",
		"connector_type":   "websocket",
		"connector_config": map[string]any{"url": "wss://example.com/ws"},
		"extra_field":      "collected",
	}}})
	cc := c.Sessions[0].ConnectorConfig
	if cc["url"] != "wss://example.com/ws" || cc["extra_field"] != "collected" {
		t.Errorf("connector_config merge wrong: %+v", cc)
	}
}

func TestProfileStoreWriteFailure(t *testing.T) {
	dir := t.TempDir()
	// Make a regular file, then root the store at a path *under* it so MkdirAll
	// (and hence writeSync) fails.
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	store := NewFileProfileStore(filepath.Join(blocker, "nested"))
	if _, err := store.CreateProfile(sampleProfile("p", "a", "private")); err == nil {
		t.Errorf("CreateProfile succeeded despite unwritable store")
	}
}

func TestProfileStoreReadError(t *testing.T) {
	dir := t.TempDir()
	store := NewFileProfileStore(dir)
	// Make profiles.json a directory so ReadFile fails with a non-NotExist error.
	if err := os.Mkdir(filepath.Join(dir, "profiles.json"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ListProfiles(nil); err == nil {
		t.Errorf("read error not surfaced")
	}
	if _, err := store.CreateProfile(sampleProfile("p", "a", "private")); err == nil {
		t.Errorf("CreateProfile read error not surfaced")
	}
	if _, err := store.UpdateProfile("p", map[string]any{"name": "x"}); err == nil {
		t.Errorf("UpdateProfile read error not surfaced")
	}
}

func TestProfileUpdateWriteFailurePropagates(t *testing.T) {
	dir := t.TempDir()
	store := NewFileProfileStore(dir)
	if _, err := store.CreateProfile(sampleProfile("p", "a", "private")); err != nil {
		t.Fatal(err)
	}
	// Replace the profiles file's directory writability by turning the file into
	// a directory path conflict: make profiles.json.tmp a directory so Rename
	// during writeSync fails on the next update.
	if err := os.Mkdir(filepath.Join(dir, "profiles.json.tmp"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := store.UpdateProfile("p", map[string]any{"name": "x"}); err == nil {
		t.Errorf("UpdateProfile write failure not propagated")
	}
	if _, err := store.DeleteProfile("p"); err == nil {
		t.Errorf("DeleteProfile write failure not propagated")
	}
}

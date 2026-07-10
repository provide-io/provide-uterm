//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestValidatorHelpersDirect(t *testing.T) {
	// connector_type "" → shell
	if ct, err := validateConnectorType("", "s1"); err != nil || ct != "shell" {
		t.Errorf("empty connector_type: %v %q", err, ct)
	}
	// <unknown> label branches (empty session id)
	if _, err := validateInputMode("bad", ""); err == nil || !strings.Contains(err.Error(), "<unknown>") {
		t.Errorf("input_mode unknown label: %v", err)
	}
	if _, err := validateVisibility("secret", ""); err == nil || !strings.Contains(err.Error(), "<unknown>") {
		t.Errorf("visibility unknown label: %v", err)
	}
	if _, err := validateConnectorType("bogus", ""); err == nil || !strings.Contains(err.Error(), "<unknown>") {
		t.Errorf("connector_type unknown label: %v", err)
	}
	// displayName empty→sessionID and non-empty passthrough
	if validateDisplayName("", "s1") != "s1" || validateDisplayName("Name", "s1") != "Name" {
		t.Errorf("validateDisplayName wrong")
	}
	// displayNameInput nil branch
	if displayNameInput(map[string]any{"display_name": nil}) != "" {
		t.Errorf("displayNameInput nil branch")
	}
	if displayNameInput(map[string]any{"display_name": "X"}) != "X" {
		t.Errorf("displayNameInput value branch")
	}
}

func TestValidateRecordingStoreAndWebhook(t *testing.T) {
	if err := validateRecording(&RecordingConfig{ControlChannelMode: "exclude", StoreType: "bogus"}); err == nil {
		t.Errorf("bad store_type accepted")
	}
	if err := validateRecording(&RecordingConfig{
		ControlChannelMode: "exclude", StoreType: "webhook", WebhookURL: sp("http://evil.com/x"),
	}); err == nil {
		t.Errorf("insecure recording webhook_url accepted")
	}
}

func TestDeepMergeNested(t *testing.T) {
	base := map[string]any{"a": map[string]any{"x": 1, "y": 2}, "b": 3}
	over := map[string]any{"a": map[string]any{"y": 20, "z": 30}, "c": 4}
	got := deepMerge(base, over)
	inner := got["a"].(map[string]any)
	if inner["x"] != 1 || inner["y"] != 20 || inner["z"] != 30 || got["b"] != 3 || got["c"] != 4 {
		t.Errorf("deepMerge nested wrong: %+v", got)
	}
	// override replaces when base value is not a map
	got2 := deepMerge(map[string]any{"a": 1}, map[string]any{"a": map[string]any{"x": 1}})
	if _, ok := got2["a"].(map[string]any); !ok {
		t.Errorf("deepMerge non-map base replace wrong")
	}
}

func TestLoadServerConfigTOMLParseError(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "bad.toml")
	if err := os.WriteFile(p, []byte("this = = broken"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadServerConfig(p); err == nil {
		t.Errorf("malformed toml accepted")
	}
}

func TestProfileUpdateAllMutableFields(t *testing.T) {
	store := NewFileProfileStore(t.TempDir())
	host, user := "h", "u"
	p := ConnectionProfile{ProfileID: "p", Owner: "a", Name: "N", ConnectorType: "ssh",
		Host: &host, Username: &user, Visibility: "private", CreatedAt: 1, UpdatedAt: 1}
	if _, err := store.CreateProfile(p); err != nil {
		t.Fatal(err)
	}
	updated, err := store.UpdateProfile("p", map[string]any{
		"host": "newhost", "port": int64(2222), "username": "newuser",
		"tags": []any{"t1"}, "input_mode": "hijack", "recording_enabled": true, "visibility": "shared",
	})
	if err != nil || updated == nil {
		t.Fatal(err)
	}
	if *updated.Host != "newhost" || updated.Port == nil || *updated.Port != 2222 ||
		*updated.Username != "newuser" || len(updated.Tags) != 1 || updated.InputMode != "hijack" ||
		!updated.RecordingEnabled || updated.Visibility != "shared" {
		t.Errorf("update fields wrong: %+v", updated)
	}
	// host update to a non-string clears it (optString nil path); port non-int clears.
	cleared, err := store.UpdateProfile("p", map[string]any{"host": 123, "port": "x"})
	if err != nil || cleared == nil {
		t.Fatal(err)
	}
	if cleared.Host != nil || cleared.Port != nil {
		t.Errorf("non-string host/port not cleared: %+v", cleared)
	}
}

func TestProfileValidateDefaults(t *testing.T) {
	p := &ConnectionProfile{ConnectorType: "ssh"}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if p.InputMode != "open" || p.Visibility != "private" || p.Tags == nil {
		t.Errorf("validate defaults wrong: %+v", p)
	}
	bad := &ConnectionProfile{ConnectorType: "ssh", InputMode: "bogus"}
	if err := bad.Validate(); err == nil {
		t.Errorf("bad input_mode accepted")
	}
	bad2 := &ConnectionProfile{ConnectorType: "ssh", Visibility: "bogus"}
	if err := bad2.Validate(); err == nil {
		t.Errorf("bad visibility accepted")
	}
}

func TestProfileStoreCorruptFile(t *testing.T) {
	dir := t.TempDir()
	store := NewFileProfileStore(dir)
	if err := os.WriteFile(filepath.Join(dir, "profiles.json"), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ListProfiles(nil); err == nil || !strings.Contains(err.Error(), "corrupt") {
		t.Errorf("corrupt store not reported: %v", err)
	}
	if _, err := store.GetProfile("x"); err == nil {
		t.Errorf("GetProfile on corrupt store did not error")
	}
	if _, err := store.DeleteProfile("x"); err == nil {
		t.Errorf("DeleteProfile on corrupt store did not error")
	}
}

func TestConfigValidatorErrorsThroughMapping(t *testing.T) {
	cases := []map[string]any{
		{"security": map[string]any{"mode": "bogus"}},
		{"tunnel": map[string]any{"token_transport": "bogus"}},
		{"pam": map[string]any{"mode": "bogus"}},
		{"governance": map[string]any{"policy_webhook_url": "http://evil.com/x"}},
		{"audit": map[string]any{"chain_enabled": true}},
	}
	for i, data := range cases {
		if _, err := ConfigFromMapping(data); err == nil {
			t.Errorf("case %d: expected validation error", i)
		}
	}
}

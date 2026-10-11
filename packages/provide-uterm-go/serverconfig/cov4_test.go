//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"errors"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func TestDecodeSectionDefaultsMarshalError(t *testing.T) {
	// A dst whose current value cannot be JSON-encoded surfaces structToMap's error.
	dst := &struct {
		F float64 `json:"f"`
	}{F: math.NaN()}
	if err := decodeSection(dst, map[string]any{"f": 1.0}); err == nil {
		t.Fatal("expected marshal error from NaN default")
	}
}

func TestDecodeSectionMergedMarshalError(t *testing.T) {
	// A user value that cannot be JSON-encoded (TOML allows nan) fails the re-encode.
	dst := &struct {
		F float64 `json:"f"`
	}{}
	err := decodeSection(dst, map[string]any{"f": math.Inf(1)})
	if err == nil {
		t.Fatal("expected marshal error from +Inf user value")
	}
	if dst.F != 0 {
		t.Fatalf("dst mutated on error: %v", dst.F)
	}
}

func TestLoadServerConfigAbsError(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "server.toml")
	if err := os.WriteFile(cfgPath, []byte("[recording]\ndirectory = \"rec\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	sentinel := errors.New("getwd failed")
	orig := absPath
	absPath = func(string) (string, error) { return "", sentinel }
	t.Cleanup(func() { absPath = orig })
	cfg, err := LoadServerConfig(cfgPath)
	if !errors.Is(err, sentinel) || cfg != nil {
		t.Fatalf("LoadServerConfig = %v, %v; want sentinel error", cfg, err)
	}
}

func TestWriteSyncMkdirError(t *testing.T) {
	// A dangling symlink as the store directory: reading profiles.json through
	// it is "not exist" (an empty store), but MkdirAll cannot create a
	// directory where the link already sits.
	dir := t.TempDir()
	link := filepath.Join(dir, "store")
	if err := os.Symlink(filepath.Join(dir, "missing", "target"), link); err != nil {
		t.Fatal(err)
	}
	s := NewFileProfileStore(link)
	p, err := s.CreateProfile(ConnectionProfile{ConnectorType: "ssh"})
	if err == nil || p != nil {
		t.Fatalf("CreateProfile = %v, %v; want MkdirAll error", p, err)
	}
}

func TestUpdateProfileInvalidUpdate(t *testing.T) {
	s := NewFileProfileStore(t.TempDir())
	if _, err := s.CreateProfile(ConnectionProfile{ProfileID: "p", ConnectorType: "ssh"}); err != nil {
		t.Fatal(err)
	}
	p, err := s.UpdateProfile("p", map[string]any{"visibility": "public"})
	if err == nil || p != nil {
		t.Fatalf("UpdateProfile = %v, %v; want visibility literal error", p, err)
	}
	got, err := s.GetProfile("p")
	if err != nil || got == nil || got.Visibility != "private" {
		t.Fatalf("invalid update persisted: %+v, %v", got, err)
	}
}

func TestWriteSyncMarshalError(t *testing.T) {
	s := NewFileProfileStore(t.TempDir())
	p, err := s.CreateProfile(ConnectionProfile{ProfileID: "p", ConnectorType: "ssh", CreatedAt: math.NaN()})
	if err == nil || p != nil {
		t.Fatalf("CreateProfile = %v, %v; want marshal error", p, err)
	}
	if _, statErr := os.Stat(s.path()); !os.IsNotExist(statErr) {
		t.Fatalf("store file written despite marshal error: %v", statErr)
	}
}

func TestWriteSyncTempWriteError(t *testing.T) {
	dir := t.TempDir()
	s := NewFileProfileStore(dir)
	// The temp path is occupied by a directory, so WriteFile fails.
	if err := os.Mkdir(s.path()+".tmp", 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CreateProfile(ConnectionProfile{ProfileID: "p", ConnectorType: "ssh"}); err == nil {
		t.Fatal("expected temp-file write error")
	}
}

func TestCreateProfileCorruptStore(t *testing.T) {
	dir := t.TempDir()
	s := NewFileProfileStore(dir)
	if err := os.WriteFile(s.path(), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if p, err := s.CreateProfile(ConnectionProfile{ConnectorType: "ssh"}); err == nil || p != nil {
		t.Fatalf("CreateProfile = %v, %v; want corrupt-store error", p, err)
	}
}

func TestUpdateProfileWriteError(t *testing.T) {
	dir := t.TempDir()
	s := NewFileProfileStore(dir)
	if _, err := s.CreateProfile(ConnectionProfile{ProfileID: "p", ConnectorType: "ssh"}); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(s.path()+".tmp", 0o755); err != nil {
		t.Fatal(err)
	}
	p, err := s.UpdateProfile("p", map[string]any{"name": "renamed"})
	if err == nil || p != nil {
		t.Fatalf("UpdateProfile = %v, %v; want write error", p, err)
	}
	got, err := s.GetProfile("p")
	if err != nil || got == nil || got.Name == "renamed" {
		t.Fatalf("store changed despite failed write: %+v, %v", got, err)
	}
}

func TestApplyCfAccessTeamDomainOnlySchemeAndPath(t *testing.T) {
	// A value that trims to nothing after stripping scheme/path/suffix leaves
	// the JWT settings untouched.
	a := &AuthConfig{CfAccessTeamDomain: "https:///cdn-cgi"}
	applyCfAccessTeamDomain(a)
	if a.JWTJWKSURL != nil || a.JWTIssuer != "" {
		t.Fatalf("auth mutated: jwks=%v issuer=%q", a.JWTJWKSURL, a.JWTIssuer)
	}
}

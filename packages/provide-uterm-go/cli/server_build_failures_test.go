//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// buildFailureConfig returns a default config with no auto-start sessions, so
// only the failure under test can stop the build.
func buildFailureConfig() *serverconfig.UtermServerConfig {
	cfg := serverconfig.DefaultServerConfig()
	cfg.Sessions = nil
	return cfg
}

// TestBuildServerRejectsUnbuildableAuthenticator drives the authenticator
// construction failure: a webhook IdP configured with an on_failure mode the
// provider refuses. The build must surface that rather than starting a server
// with no working identity provider.
func TestBuildServerRejectsUnbuildableAuthenticator(t *testing.T) {
	cfg := buildFailureConfig()
	url := "https://idp.invalid/verify"
	cfg.Auth.IdentityProvider = "webhook"
	cfg.Auth.WebhookIDPURL = &url
	cfg.Auth.WebhookIDPOnFailure = "sometimes"

	bundle, err := buildServerFromConfig(context.Background(), cfg, "")
	if err == nil {
		t.Fatal("expected the build to refuse an unbuildable webhook IdP")
	}
	if bundle != nil {
		t.Fatal("no bundle may be returned for a failed build")
	}
	if !strings.Contains(err.Error(), "on_failure") {
		t.Fatalf("error = %v", err)
	}
}

// TestBuildServerRejectsUnopenableControlPlane points the sqlite control plane
// at a path that is a directory, so opening it genuinely fails.
func TestBuildServerRejectsUnopenableControlPlane(t *testing.T) {
	dir := t.TempDir()
	// A directory can never be opened as a SQLite database file.
	dbPath := filepath.Join(dir, "not-a-file")
	if err := os.MkdirAll(dbPath, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	cfg := buildFailureConfig()
	cfg.ControlPlane.Backend = "sqlite"
	cfg.ControlPlane.DatabaseURL = &dbPath

	bundle, err := buildServerFromConfig(context.Background(), cfg, "")
	if err == nil {
		if bundle != nil {
			_ = bundle.engine.Close(context.Background())
		}
		t.Fatal("expected the build to refuse an unopenable control plane")
	}
	if bundle != nil {
		t.Fatal("no bundle may be returned for a failed build")
	}
}

// TestTokenFileDefaultFallsBackWithoutHome covers the fallback used when the
// home directory cannot be resolved.
func TestTokenFileDefaultFallsBackWithoutHome(t *testing.T) {
	if got := tokenFileDefault(); got == "" {
		t.Fatal("resolved token file must not be empty")
	}
	t.Setenv("HOME", "")
	if got := tokenFileDefault(); got != ".uterm/session_token" {
		t.Fatalf("fallback = %q", got)
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// The server keeps PAM capture sessions only in the state directory its
// config names, and refuses to build without one.
func TestABuildWithPersistenceButNoStateDirectoryIsRefused(t *testing.T) {
	cfg := buildFailureConfig()
	cfg.SessionPersistence.PersistCapture = true
	bundle, err := buildServerFromConfig(context.Background(), cfg, "")
	if err == nil || bundle != nil || err.Error() != serverconfig.StateDirRequired {
		t.Fatalf("build = %v, %v", bundle, err)
	}
}

// At startup a kept session's socket must pass PAM's confinement: one outside
// the capture socket directory is not bound, and its live file goes.
func TestABuildRestoresOnlyConfinedCaptureSessions(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "capture-sessions")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	owner := "alice"
	id := "pam-alice-capture-1-2"
	def := serverconfig.SessionDefinition{
		SessionID: id, DisplayName: id, ConnectorType: "pty_capture",
		ConnectorConfig: map[string]any{"socket_path": "/elsewhere/s.sock"}, InputMode: "open",
		Tags: []string{}, Owner: &owner, Visibility: "private", CreatedAt: time.Now().UTC(),
		AutoTransferIdleS: 30, KeystrokeQueue: "display",
	}
	kept := capturestate.New(dir, true, 0, 0, slog.New(slog.NewTextHandler(io.Discard, nil)))
	kept.Track(def, "i")
	kept.Flush(id)
	live := filepath.Join(dir, "live", id+".json")
	if _, err := os.Stat(live); err != nil {
		t.Fatal(err)
	}
	cfg := buildFailureConfig()
	confined := "/run/uterm-capture"
	cfg.Pam.CaptureSocketDir = &confined
	cfg.SessionPersistence = serverconfig.SessionPersistenceConfig{PersistCapture: true, StateDir: &dir}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	bundle, err := buildServerFromConfig(ctx, cfg, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bundle.engine.Close(context.Background()) })
	if _, ok := bundle.registry.GetDefinition(ctx, id); ok {
		t.Fatal("an unconfined socket was restored")
	}
	if _, err := os.Stat(live); !os.IsNotExist(err) {
		t.Fatalf("the live file stayed: %v", err)
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// TestBuildRecordingStore covers every store-type selection branch.
func TestBuildRecordingStore(t *testing.T) {
	cfg := serverconfig.DefaultServerConfig()

	cfg.Recording.StoreType = "local"
	cfg.Recording.Directory = t.TempDir()
	if _, ok := buildRecordingStore(cfg).(*recording.LocalFileStore); !ok {
		t.Errorf("local → want *LocalFileStore, got %T", buildRecordingStore(cfg))
	}

	cfg.Recording.StoreType = "memory"
	if _, ok := buildRecordingStore(cfg).(*recording.InMemoryStore); !ok {
		t.Errorf("memory → want *InMemoryStore, got %T", buildRecordingStore(cfg))
	}

	cfg.Recording.StoreType = "null"
	if _, ok := buildRecordingStore(cfg).(recording.NullStore); !ok {
		t.Errorf("null → want NullStore, got %T", buildRecordingStore(cfg))
	}

	// A webhook store with somewhere to deliver is one; without a URL the
	// reference falls back to the local file store, never to recording
	// nothing.
	cfg.Recording.StoreType = "webhook"
	if _, ok := buildRecordingStore(cfg).(*recording.LocalFileStore); !ok {
		t.Errorf("webhook without a url → want *LocalFileStore, got %T", buildRecordingStore(cfg))
	}
	empty := ""
	cfg.Recording.WebhookURL = &empty
	if _, ok := buildRecordingStore(cfg).(*recording.LocalFileStore); !ok {
		t.Errorf("webhook with an empty url → want *LocalFileStore, got %T", buildRecordingStore(cfg))
	}
	url, secret := "https://recorder.example/rec", "s3cret" //nolint:gosec // test fixture
	cfg.Recording.WebhookURL, cfg.Recording.WebhookSecret = &url, &secret
	ws, ok := buildRecordingStore(cfg).(*server.WebhookRecordingStore)
	if !ok {
		t.Fatalf("webhook with a url → want *server.WebhookRecordingStore, got %T", buildRecordingStore(cfg))
	}
	if ws.URL != url || ws.Secret != secret || ws.Timeout != 2*time.Second {
		t.Errorf("webhook store = %+v", ws)
	}

	// Any other value is the reference's last branch: the local file store.
	cfg.Recording.StoreType = "none"
	if _, ok := buildRecordingStore(cfg).(*recording.LocalFileStore); !ok {
		t.Errorf("default → want *LocalFileStore, got %T", buildRecordingStore(cfg))
	}
}

// TestRunServerNilContext covers the nil-context guard: runServer defaults to
// context.Background() before building, then fails fast on a bad config path.
func TestRunServerNilContext(t *testing.T) {
	//nolint:staticcheck // deliberately passing a nil context to exercise the guard
	if err := runServer(nil, "/no/such/config.toml", "", 0, ""); err == nil {
		t.Fatal("bad config should error even with a nil context")
	}
}

// TestBuildServerPamIntegration covers the opt-in PAM branch in buildServer: when
// pam.notify_socket is configured, buildServer spawns the integration goroutine.
// The socket path is unbindable, so pam.Run returns an error and the goroutine
// walks the warn-log branch.
func TestBuildServerPamIntegration(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "server.toml")
	body := `
[server]
host = "127.0.0.1"
port = 0

[auth]
mode = "dev_token"

[pam]
notify_socket = "/nonexistent-uterm-dir/pam.sock"
`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	bundle, err := buildServer(ctx, path, "", 0, "")
	if err != nil {
		t.Fatalf("buildServer with pam: %v", err)
	}
	defer func() { _ = bundle.engine.Close(context.Background()) }()

	// The integration goroutine runs pam.Run synchronously (an unbindable socket
	// fails immediately); give it a brief moment to reach the warn-log branch.
	time.Sleep(100 * time.Millisecond)
	cancel()
}

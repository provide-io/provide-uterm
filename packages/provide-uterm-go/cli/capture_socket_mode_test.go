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
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// pam.capture_socket_mode holds for every capture session the registry starts,
// however it was made: by PAM, through the sessions API with a socket of its
// own, or re-created from its file after a restart. The mode is the server's,
// never the session's.

func modeRegistry(t *testing.T, mode int) *SessionRegistryImpl {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	cfg.Pam.CaptureSocketMode = mode
	r := NewSessionRegistry(cfg)
	t.Cleanup(func() { r.Shutdown(context.Background()) })
	return r
}

// boundMode starts the session and answers its capture socket's permissions.
func boundMode(t *testing.T, r *SessionRegistryImpl, id, socket string) os.FileMode {
	t.Helper()
	st, err := r.StartSession(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if st.LastError != nil {
		t.Fatalf("start failed: %s", *st.LastError)
	}
	info, err := os.Stat(socket)
	if err != nil {
		t.Fatal(err)
	}
	return info.Mode().Perm()
}

func TestAPamCaptureSocketIsBoundWithTheConfiguredMode(t *testing.T) {
	for _, tc := range []struct {
		mode int
		want os.FileMode
	}{
		{0o600, 0o600},
		{0o660, 0o660},
		// A config built in code rather than loaded leaves the mode zero,
		// which is the reference's default.
		{0, 0o600},
	} {
		r := modeRegistry(t, tc.mode)
		socket := shortCaptureDir(t) + "/uterm-cap-11.sock"
		if _, err := r.CreateSession(context.Background(), capturePayload("pam-zed-capture-11-1", socket)); err != nil {
			t.Fatal(err)
		}
		if got := boundMode(t, r, "pam-zed-capture-11-1", socket); got != tc.want {
			t.Fatalf("mode %o: perms = %o, want %o", tc.mode, got, tc.want)
		}
	}
}

func TestAnAPICaptureSocketIsBoundWithTheServersModeNotItsOwn(t *testing.T) {
	for _, tc := range []struct {
		mode  int
		asked any
		want  os.FileMode
	}{
		{0o660, nil, 0o660},
		{0o600, nil, 0o600},
		{0o600, float64(0o660), 0o600},
	} {
		r := modeRegistry(t, tc.mode)
		socket := shortCaptureDir(t) + "/uterm-play-7.sock"
		config := map[string]any{"socket_path": socket, "cols": 80, "rows": 24}
		if tc.asked != nil {
			config["socket_mode"] = tc.asked
		}
		payload := map[string]any{"session_id": "play-7", "connector_type": "pty_capture", "connector_config": config}
		if _, err := r.CreateSession(context.Background(), payload); err != nil {
			t.Fatal(err)
		}
		if got := boundMode(t, r, "play-7", socket); got != tc.want {
			t.Fatalf("%+v: perms = %o", tc, got)
		}
		// What is stored is what was asked for; the mode is added only to
		// what the connector is given.
		def, _ := r.GetDefinition(context.Background(), "play-7")
		if _, stored := def.ConnectorConfig["socket_mode"]; stored != (tc.asked != nil) {
			t.Fatalf("%+v: stored config %v", tc, def.ConnectorConfig)
		}
	}
}

func TestARestoredCaptureSocketIsBoundWithTheConfiguredMode(t *testing.T) {
	dir := t.TempDir()
	id := liveID(t)
	socket := shortCaptureDir(t) + "/uterm-cap.sock"
	keptBeforeRestart(t, dir, id, socket)
	if _, stored := keptSession(t, dir, id)["connector_config"].(map[string]any)["socket_mode"]; stored {
		t.Fatal("the kept session carries a socket mode")
	}

	r := modeRegistry(t, 0o660)
	r.SetCaptureState(capturestate.New(dir, true, 7, 100, slog.New(slog.NewTextHandler(io.Discard, nil))))
	r.RestoreCaptureSessions(context.Background(), func(string) bool { return true })
	if got := boundMode(t, r, id, socket); got != 0o660 {
		t.Fatalf("perms = %o, want 660", got)
	}
}

// Only a capture connector is given the mode, on a copy of its config; a
// capture definition with no config at all gets one holding just the mode, so
// the connector refuses it for its missing socket rather than the start
// panicking on a nil map.
func TestTheModeIsAddedOnlyToACaptureConnectorsConfig(t *testing.T) {
	stored := map[string]any{"socket_path": "/run/x.sock"}
	capture := withCaptureSocketMode(serverconfig.SessionDefinition{ConnectorType: "pty_capture", ConnectorConfig: stored}, 0o660)
	if capture.ConnectorConfig["socket_mode"] != 0o660 || len(stored) != 1 {
		t.Fatalf("given %v, stored %v", capture.ConnectorConfig, stored)
	}
	shell := withCaptureSocketMode(serverconfig.SessionDefinition{ConnectorType: "shell", ConnectorConfig: stored}, 0o660)
	if _, given := shell.ConnectorConfig["socket_mode"]; given {
		t.Fatalf("shell given %v", shell.ConnectorConfig)
	}
	bare := withCaptureSocketMode(serverconfig.SessionDefinition{ConnectorType: "pty_capture"}, 0o600)
	if len(bare.ConnectorConfig) != 1 || bare.ConnectorConfig["socket_mode"] != 0o600 {
		t.Fatalf("bare given %v", bare.ConnectorConfig)
	}
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// writeScenario drops a scenario file in a temp dir and returns its path.
func writeScenario(t *testing.T, name, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write scenario: %v", err)
	}
	return path
}

func TestExecuteClient(t *testing.T) {
	fs := newFakeServer(t, jsonHandler(200, `{"status":"ok"}`))
	path := writeScenario(t, "010_health.json",
		`{"id":"010_health","title":"h","steps":[{"id":"health","action":"health"}]}`)

	var stdout, stderr bytes.Buffer
	code := Execute(context.Background(), []string{
		"client", "--base-url", fs.URL, "--token", "tok", "--scenario", path,
	}, nil, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit %d, stderr=%s", code, stderr.String())
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr should be quiet on success: %s", stderr.String())
	}
	var r Result
	if err := json.Unmarshal(stdout.Bytes(), &r); err != nil {
		t.Fatalf("stdout %q: %v", stdout.String(), err)
	}
	if r.Status != StatusCompleted || r.Role != RoleClient {
		t.Fatalf("result = %+v", r)
	}
}

func TestExecuteClientReportsAnErroredScenarioOnStdoutAndExitsZero(t *testing.T) {
	// A malformed scenario is still a report: the harness reads the verdict
	// from the JSON, so the exit code says only that a report was produced.
	path := writeScenario(t, "011_broken.json", `{"id":"011_broken","steps":[]}`)
	var stdout, stderr bytes.Buffer
	code := Execute(context.Background(), []string{
		"client", "--base-url", "http://127.0.0.1", "--scenario", path,
	}, nil, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit %d, stderr=%s", code, stderr.String())
	}
	var r Result
	if err := json.Unmarshal(stdout.Bytes(), &r); err != nil {
		t.Fatalf("stdout %q: %v", stdout.String(), err)
	}
	if r.Status != StatusError || r.ScenarioID != "011_broken" {
		t.Fatalf("result = %+v", r)
	}
}

func TestExecuteUsageErrorsKeepStdoutClean(t *testing.T) {
	cases := [][]string{
		{},                                   // no subcommand
		{"teleport"},                         // unknown subcommand
		{"client", "--scenario", "x.json"},   // missing --base-url
		{"client", "--base-url", "http://x"}, // missing --scenario
		{"client", "--base-url"},             // flag without a value
		{"serve", "--auth"},                  // flag without a value
	}
	for _, args := range cases {
		t.Run(strings.Join(args, " "), func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := Execute(context.Background(), args, nil, &stdout, &stderr); code != 1 {
				t.Fatalf("exit = %d, want 1", code)
			}
			if stdout.Len() != 0 {
				t.Fatalf("stdout must stay a protocol channel, got %q", stdout.String())
			}
			if stderr.Len() == 0 {
				t.Fatal("a usage error must say something on stderr")
			}
		})
	}
}

func TestExecuteHelpGoesToStderr(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := Execute(context.Background(), []string{"--help"}, nil, &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d", code)
	}
	if stdout.Len() != 0 {
		t.Fatalf("help must not touch stdout, got %q", stdout.String())
	}
	if !strings.Contains(stderr.String(), "serve") || !strings.Contains(stderr.String(), "client") {
		t.Fatalf("help should list both roles: %s", stderr.String())
	}
}

func TestExecuteServe(t *testing.T) {
	isolateDevTokenFor(t)
	stdinR, stdinW, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	t.Cleanup(func() { _ = stdinR.Close() })

	out := &syncWriter{}
	var stderr bytes.Buffer
	done := make(chan int, 1)
	go func() {
		done <- Execute(context.Background(), []string{"serve", "--auth", "dev_token"}, stdinR, out, &stderr)
	}()

	deadline := time.After(30 * time.Second)
	for !strings.Contains(out.String(), "base_url") {
		select {
		case code := <-done:
			t.Fatalf("serve exited early with %d: %s", code, stderr.String())
		case <-deadline:
			t.Fatal("timed out waiting for the handshake")
		case <-time.After(20 * time.Millisecond):
		}
	}

	var line ServerLine
	if err := json.Unmarshal([]byte(out.String()), &line); err != nil {
		t.Fatalf("handshake %q: %v", out.String(), err)
	}
	if line.Role != RoleServer || line.Token == "" {
		t.Fatalf("handshake = %+v", line)
	}
	resp, err := http.Get(line.BaseURL + "/api/health") //nolint:noctx // test
	if err != nil {
		t.Fatalf("health: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status = %d", resp.StatusCode)
	}

	// Closing stdin is the ordinary shutdown.
	_ = stdinW.Close()
	select {
	case code := <-done:
		if code != 0 {
			t.Fatalf("serve exit = %d: %s", code, stderr.String())
		}
	case <-time.After(30 * time.Second):
		t.Fatal("serve ignored stdin EOF")
	}
}

func TestExecuteServeReportsAStartupFailure(t *testing.T) {
	isolateDevTokenFor(t)
	var stdout, stderr bytes.Buffer
	code := Execute(context.Background(), []string{
		"serve", "--config", filepath.Join(t.TempDir(), "missing.toml"),
	}, nil, &stdout, &stderr)
	if code != 1 {
		t.Fatalf("exit = %d, want 1", code)
	}
	if stdout.Len() != 0 {
		t.Fatalf("a failed start must not write a handshake, got %q", stdout.String())
	}
	if !strings.Contains(stderr.String(), "error:") {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestServeAcceptsButIgnoresScenario(t *testing.T) {
	// The protocol allows `serve [--scenario FILE]`; the server role performs
	// no steps, so the flag must parse and do nothing.
	cmd := NewRootCmd(nil, nil)
	serve, _, err := cmd.Find([]string{"serve"})
	if err != nil {
		t.Fatalf("find serve: %v", err)
	}
	if serve.Flags().Lookup("scenario") == nil {
		t.Fatal("serve must accept --scenario")
	}
	for _, name := range []string{"auth", "config"} {
		if serve.Flags().Lookup(name) == nil {
			t.Fatalf("serve must accept --%s", name)
		}
	}
}

func TestMustPanicsOnAProgrammingMistake(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("must should have panicked")
		}
	}()
	must(os.ErrInvalid)
}

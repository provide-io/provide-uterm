//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"testing"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/mcptest"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// fakeUtermServer is an httptest server standing in for the provide-uterm REST
// API so the integration test exercises the real HijackClient wire path.
func fakeUtermServer(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, map[string]any{"status": "healthy"})
	})
	mux.HandleFunc("/api/sessions", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, []any{map[string]any{"id": "s1", "state": "running"}})
	})
	return httptest.NewServer(mux)
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// startMCP boots an in-process MCP server (real client -> httptest backend)
// with the given default role and returns a connected client.
func startMCP(t *testing.T, baseURL, role string) *mcptest.Server {
	t.Helper()
	c := client.NewHijackClient(baseURL)
	auth := &AuthorizationContext{DefaultPrincipal: newPrincipal("local", role)}
	tools := append(hijackTools(c, auth), sessionTools(c, auth)...)
	srv, err := mcptest.NewServer(t, tools...)
	if err != nil {
		t.Fatalf("mcptest.NewServer: %v", err)
	}
	return srv
}

// callText invokes a tool over the MCP transport and decodes its JSON text
// content into a map.
func callText(t *testing.T, srv *mcptest.Server, name string, args map[string]any) map[string]any {
	t.Helper()
	var req mcpgo.CallToolRequest
	req.Params.Name = name
	req.Params.Arguments = args
	res, err := srv.Client().CallTool(context.Background(), req)
	if err != nil {
		t.Fatalf("CallTool(%s): %v", name, err)
	}
	for _, c := range res.Content {
		if tc, ok := c.(mcpgo.TextContent); ok {
			var m map[string]any
			if err := json.Unmarshal([]byte(tc.Text), &m); err != nil {
				t.Fatalf("decode %s result %q: %v", name, tc.Text, err)
			}
			return m
		}
	}
	t.Fatalf("no text content in %s result", name)
	return nil
}

func TestIntegrationListTools(t *testing.T) {
	ts := fakeUtermServer(t)
	defer ts.Close()
	srv := startMCP(t, ts.URL, "admin")
	defer srv.Close()

	lt, err := srv.Client().ListTools(context.Background(), mcpgo.ListToolsRequest{})
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}
	if len(lt.Tools) != 21 {
		t.Fatalf("expected 21 tools, got %d", len(lt.Tools))
	}
	got := make([]string, 0, len(lt.Tools))
	for _, tool := range lt.Tools {
		got = append(got, tool.Name)
	}
	want := append([]string(nil), AllToolNames...)
	sort.Strings(got)
	sort.Strings(want)
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("tool set mismatch at %d: got %q want %q\nall got=%v", i, got[i], want[i], got)
		}
	}
}

func TestIntegrationSchemasRequireKeyArgs(t *testing.T) {
	ts := fakeUtermServer(t)
	defer ts.Close()
	srv := startMCP(t, ts.URL, "admin")
	defer srv.Close()

	lt, err := srv.Client().ListTools(context.Background(), mcpgo.ListToolsRequest{})
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}
	required := map[string][]string{
		"hijack_begin":        {"worker_id"},
		"hijack_send":         {"worker_id", "hijack_id", "keys"},
		"session_create":      {"connector_type"},
		"fanout_group_create": {"session_ids"},
		"session_annotate":    {"session_id", "label"},
	}
	byName := map[string]mcpgo.Tool{}
	for _, tool := range lt.Tools {
		byName[tool.Name] = tool
	}
	for name, reqs := range required {
		tool, ok := byName[name]
		if !ok {
			t.Fatalf("tool %q missing", name)
		}
		have := map[string]bool{}
		for _, r := range tool.InputSchema.Required {
			have[r] = true
		}
		for _, r := range reqs {
			if !have[r] {
				t.Errorf("tool %q must require %q (required=%v)", name, r, tool.InputSchema.Required)
			}
			if _, ok := tool.InputSchema.Properties[r]; !ok {
				t.Errorf("tool %q schema missing property %q", name, r)
			}
		}
	}
}

func TestIntegrationInvokeAgainstBackend(t *testing.T) {
	ts := fakeUtermServer(t)
	defer ts.Close()
	srv := startMCP(t, ts.URL, "admin")
	defer srv.Close()

	health := callText(t, srv, "server_health", nil)
	if health["success"] != true || health["status"] != "healthy" {
		t.Fatalf("server_health wrong: %#v", health)
	}
	list := callText(t, srv, "session_list", nil)
	if list["success"] != true {
		t.Fatalf("session_list wrong: %#v", list)
	}
	// The array body is wrapped under "data".
	if _, ok := list["data"].([]any); !ok {
		t.Fatalf("session_list should carry data array: %#v", list)
	}
	// SSRF validation happens before any RPC and returns structured data.
	ssrf := callText(t, srv, "session_create", map[string]any{"connector_type": "ssh", "host": "127.0.0.1"})
	if ssrf["error"] != "invalid_host" || ssrf["host"] != "127.0.0.1" {
		t.Fatalf("session_create SSRF guard failed: %#v", ssrf)
	}
}

func TestIntegrationAuthorizationDenied(t *testing.T) {
	ts := fakeUtermServer(t)
	defer ts.Close()
	srv := startMCP(t, ts.URL, "viewer") // viewer cannot hijack
	defer srv.Close()

	denied := callText(t, srv, "hijack_begin", map[string]any{"worker_id": "w1"})
	if denied["error"] != "authorization_denied" || denied["required_role"] != "admin" {
		t.Fatalf("expected authorization_denied, got %#v", denied)
	}
}

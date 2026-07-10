//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"
	"testing"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// serverTool aliases the mcp-go ServerTool for brevity in tests.
type serverTool = server.ServerTool

// recordedCall captures one dispatched REST/WS call so a test can assert the
// exact method + arguments a tool routed to.
type recordedCall struct {
	Method string
	Params map[string]any
}

// fakeClient is an in-memory UtermClient that records every call and returns
// programmable responses. objResp/objErr back the map-returning methods;
// anyResp/anyErr back the any-returning methods.
type fakeClient struct {
	calls   []recordedCall
	objResp map[string]any
	objErr  error
	anyResp any
	anyErr  error
}

func (f *fakeClient) record(method string, params map[string]any) {
	f.calls = append(f.calls, recordedCall{Method: method, Params: params})
}

func (f *fakeClient) last() recordedCall {
	if len(f.calls) == 0 {
		return recordedCall{}
	}
	return f.calls[len(f.calls)-1]
}

func (f *fakeClient) obj() (map[string]any, error) { return f.objResp, f.objErr }
func (f *fakeClient) val() (any, error)            { return f.anyResp, f.anyErr }

func (f *fakeClient) Acquire(_ context.Context, workerID string, opts client.AcquireOptions) (map[string]any, error) {
	f.record("Acquire", map[string]any{"workerID": workerID, "owner": opts.Owner, "lease_s": opts.LeaseS})
	return f.obj()
}

func (f *fakeClient) Heartbeat(_ context.Context, workerID, hijackID string, leaseS int) (map[string]any, error) {
	f.record("Heartbeat", map[string]any{"workerID": workerID, "hijackID": hijackID, "lease_s": leaseS})
	return f.obj()
}

func (f *fakeClient) Snapshot(_ context.Context, workerID, hijackID string, waitMS int) (map[string]any, error) {
	f.record("Snapshot", map[string]any{"workerID": workerID, "hijackID": hijackID, "wait_ms": waitMS})
	return f.obj()
}

func (f *fakeClient) Events(_ context.Context, workerID, hijackID string, opts client.EventsOptions) (map[string]any, error) {
	f.record("Events", map[string]any{"workerID": workerID, "hijackID": hijackID, "after_seq": opts.AfterSeq, "limit": opts.Limit})
	return f.obj()
}

func (f *fakeClient) Send(_ context.Context, workerID, hijackID string, opts client.SendOptions) (map[string]any, error) {
	f.record("Send", map[string]any{
		"workerID": workerID, "hijackID": hijackID, "keys": opts.Keys,
		"expect_prompt_id": opts.ExpectPromptID, "expect_regex": opts.ExpectRegex,
		"timeout_ms": opts.TimeoutMS, "poll_interval_ms": opts.PollIntervalMS,
	})
	return f.obj()
}

func (f *fakeClient) Step(_ context.Context, workerID, hijackID string) (map[string]any, error) {
	f.record("Step", map[string]any{"workerID": workerID, "hijackID": hijackID})
	return f.obj()
}

func (f *fakeClient) Release(_ context.Context, workerID, hijackID string) (map[string]any, error) {
	f.record("Release", map[string]any{"workerID": workerID, "hijackID": hijackID})
	return f.obj()
}

func (f *fakeClient) Health(_ context.Context) (map[string]any, error) {
	f.record("Health", map[string]any{})
	return f.obj()
}

func (f *fakeClient) SetSessionMode(_ context.Context, sessionID, mode string) (map[string]any, error) {
	f.record("SetSessionMode", map[string]any{"sessionID": sessionID, "mode": mode})
	return f.obj()
}

func (f *fakeClient) SetInputMode(_ context.Context, workerID, mode string) (map[string]any, error) {
	f.record("SetInputMode", map[string]any{"workerID": workerID, "mode": mode})
	return f.obj()
}

func (f *fakeClient) DisconnectWorker(_ context.Context, workerID string) (map[string]any, error) {
	f.record("DisconnectWorker", map[string]any{"workerID": workerID})
	return f.obj()
}

func (f *fakeClient) ListSessions(_ context.Context) (any, error) {
	f.record("ListSessions", map[string]any{})
	return f.val()
}

func (f *fakeClient) GetSession(_ context.Context, sessionID string) (map[string]any, error) {
	f.record("GetSession", map[string]any{"sessionID": sessionID})
	return f.obj()
}

func (f *fakeClient) SessionSnapshot(_ context.Context, sessionID string) (any, error) {
	f.record("SessionSnapshot", map[string]any{"sessionID": sessionID})
	return f.val()
}

func (f *fakeClient) ConnectSession(_ context.Context, sessionID string) (map[string]any, error) {
	f.record("ConnectSession", map[string]any{"sessionID": sessionID})
	return f.obj()
}

func (f *fakeClient) DisconnectSession(_ context.Context, sessionID string) (map[string]any, error) {
	f.record("DisconnectSession", map[string]any{"sessionID": sessionID})
	return f.obj()
}

func (f *fakeClient) QuickConnect(_ context.Context, connectorType string, opts client.QuickConnectOptions) (map[string]any, error) {
	f.record("QuickConnect", map[string]any{"connector_type": connectorType, "display_name": opts.DisplayName, "config": opts.Config})
	return f.obj()
}

func (f *fakeClient) WatchSessionEvents(_ context.Context, sessionID string, opts client.WatchOptions) (any, error) {
	f.record("WatchSessionEvents", map[string]any{
		"sessionID": sessionID, "event_types": opts.EventTypes, "pattern": opts.Pattern,
		"timeout_ms": opts.TimeoutMS, "max_events": opts.MaxEvents,
	})
	return f.val()
}

func (f *fakeClient) Post(_ context.Context, path string, body map[string]any) (any, error) {
	f.record("Post", map[string]any{"path": path, "body": body})
	return f.val()
}

// adminAuth returns an authorization context whose default principal holds the
// admin role, so every tool is permitted by default in tool-body tests.
func adminAuth() *AuthorizationContext {
	return &AuthorizationContext{DefaultPrincipal: newPrincipal("test", "admin")}
}

// findTool returns the ServerTool with the given name, failing the test if
// absent.
func findTool(t *testing.T, tools []serverTool, name string) serverTool {
	t.Helper()
	for _, st := range tools {
		if st.Tool.Name == name {
			return st
		}
	}
	t.Fatalf("tool %q not registered", name)
	return serverTool{}
}

// reqWith builds a CallToolRequest carrying the given arguments.
func reqWith(args map[string]any) mcpgo.CallToolRequest {
	req := mcpgo.CallToolRequest{}
	req.Params.Arguments = args
	return req
}

// invoke calls a tool handler with the given arguments and returns its
// structured result dict.
func invoke(t *testing.T, st serverTool, args map[string]any) map[string]any {
	t.Helper()
	req := mcpgo.CallToolRequest{}
	req.Params.Name = st.Tool.Name
	req.Params.Arguments = args
	res, err := st.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("handler %q returned error: %v", st.Tool.Name, err)
	}
	m, ok := res.StructuredContent.(map[string]any)
	if !ok {
		t.Fatalf("handler %q result is not a map: %#v", st.Tool.Name, res.StructuredContent)
	}
	return m
}

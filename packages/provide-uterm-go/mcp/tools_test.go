//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"reflect"
	"testing"
)

// hijackFixture builds the hijack tool set backed by a fresh fake client whose
// map endpoints return a canned OK payload.
func hijackFixture() (*fakeClient, []serverTool) {
	f := &fakeClient{objResp: map[string]any{"detail": "ok"}}
	return f, hijackTools(f, adminAuth())
}

// sessionFixture builds the session tool set backed by a fresh fake client.
func sessionFixture() (*fakeClient, []serverTool) {
	f := &fakeClient{objResp: map[string]any{"detail": "ok"}, anyResp: map[string]any{"detail": "ok"}}
	return f, sessionTools(f, adminAuth())
}

func TestHijackBeginDispatch(t *testing.T) {
	f, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_begin"), map[string]any{
		"worker_id": "w1", "lease_s": 45, "owner": "alice",
	})
	if res["success"] != true || res["detail"] != "ok" {
		t.Fatalf("unexpected result: %#v", res)
	}
	call := f.last()
	if call.Method != "Acquire" {
		t.Fatalf("expected Acquire, got %q", call.Method)
	}
	if call.Params["workerID"] != "w1" || call.Params["owner"] != "alice" || call.Params["lease_s"] != 45 {
		t.Fatalf("wrong Acquire params: %#v", call.Params)
	}
}

func TestHijackBeginDefaults(t *testing.T) {
	f, tools := hijackFixture()
	invoke(t, findTool(t, tools, "hijack_begin"), map[string]any{"worker_id": "w1"})
	call := f.last()
	if call.Params["owner"] != "operator" || call.Params["lease_s"] != 90 {
		t.Fatalf("expected default owner/lease, got %#v", call.Params)
	}
}

func TestHijackBeginBadID(t *testing.T) {
	_, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_begin"), map[string]any{"worker_id": "../etc"})
	if res["error"] != "invalid_id" || res["detail"] != `invalid worker_id: '../etc'` {
		t.Fatalf("expected invalid_id rejection, got %#v", res)
	}
}

func TestHijackHeartbeatBadHijackID(t *testing.T) {
	_, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_heartbeat"), map[string]any{"worker_id": "w1", "hijack_id": "a/b"})
	if res["error"] != "invalid_id" || res["detail"] != `invalid hijack_id: 'a/b'` {
		t.Fatalf("expected hijack_id rejection, got %#v", res)
	}
}

func TestHijackReadSnapshotCleaned(t *testing.T) {
	f := &fakeClient{objResp: map[string]any{"snapshot": map[string]any{
		"screen": "\x1b[31mhello\x1b[0m\nworld", "cursor": 3, "cols": 80, "rows": 24,
	}}}
	tools := hijackTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "hijack_read"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "output": "text",
	})
	snap, ok := res["snapshot"].(map[string]any)
	if !ok {
		t.Fatalf("no snapshot in result: %#v", res)
	}
	if snap["screen"] != "hello\nworld" {
		t.Fatalf("expected ANSI stripped text-only screen, got %#v", snap)
	}
	if _, hasCursor := snap["cursor"]; hasCursor {
		t.Fatalf("text output must not carry layout metadata: %#v", snap)
	}
	if f.last().Method != "Snapshot" {
		t.Fatalf("expected Snapshot call, got %q", f.last().Method)
	}
}

func TestHijackReadEventsMode(t *testing.T) {
	f := &fakeClient{objResp: map[string]any{"events": []any{}}}
	tools := hijackTools(f, adminAuth())
	invoke(t, findTool(t, tools, "hijack_read"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "mode": "events", "after_seq": 5, "limit": 10,
	})
	call := f.last()
	if call.Method != "Events" || call.Params["after_seq"] != 5 || call.Params["limit"] != 10 {
		t.Fatalf("wrong Events dispatch: %#v", call)
	}
}

func TestHijackReadTailRawTrim(t *testing.T) {
	f := &fakeClient{objResp: map[string]any{"snapshot": map[string]any{"screen": "l1\nl2\nl3\nl4", "cols": 80}}}
	tools := hijackTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "hijack_read"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "output": "raw", "tail_lines": 2,
	})
	snap := res["snapshot"].(map[string]any)
	if snap["screen"] != "l3\nl4" {
		t.Fatalf("raw+tail should keep last 2 lines, got %#v", snap["screen"])
	}
	if snap["cols"] != 80 {
		t.Fatalf("raw output must preserve other fields: %#v", snap)
	}
}

func TestHijackSendSanitizesAndGuards(t *testing.T) {
	f, tools := hijackFixture()
	invoke(t, findTool(t, tools, "hijack_send"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "keys": `echo hi\n`, "expect_prompt_id": "p1",
	})
	call := f.last()
	if call.Method != "Send" {
		t.Fatalf("expected Send, got %q", call.Method)
	}
	if call.Params["keys"] != "echo hi\n" {
		t.Fatalf("keys not unescaped: %#v", call.Params["keys"])
	}
	if call.Params["expect_prompt_id"] != "p1" {
		t.Fatalf("expect_prompt_id not forwarded: %#v", call.Params)
	}
	if call.Params["timeout_ms"] != 2000 || call.Params["poll_interval_ms"] != 120 {
		t.Fatalf("expected default timeouts, got %#v", call.Params)
	}
}

func TestHijackSendRejectsBadRegex(t *testing.T) {
	f, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_send"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "keys": "x", "expect_regex": "(a+)+",
	})
	if res["error"] != "invalid_pattern" {
		t.Fatalf("expected invalid_pattern, got %#v", res)
	}
	if f.last().Method == "Send" {
		t.Fatalf("Send must not be called after pattern rejection")
	}
}

func TestHijackStepReleaseDispatch(t *testing.T) {
	f, tools := hijackFixture()
	invoke(t, findTool(t, tools, "hijack_step"), map[string]any{"worker_id": "w1", "hijack_id": "h1"})
	if f.last().Method != "Step" {
		t.Fatalf("expected Step, got %q", f.last().Method)
	}
	invoke(t, findTool(t, tools, "hijack_release"), map[string]any{"worker_id": "w1", "hijack_id": "h1"})
	if f.last().Method != "Release" {
		t.Fatalf("expected Release, got %q", f.last().Method)
	}
}

func TestServerControlTools(t *testing.T) {
	f, tools := hijackFixture()
	invoke(t, findTool(t, tools, "server_health"), map[string]any{})
	if f.last().Method != "Health" {
		t.Fatalf("expected Health, got %q", f.last().Method)
	}
	invoke(t, findTool(t, tools, "session_set_mode"), map[string]any{"session_id": "s1", "mode": "hijack"})
	if c := f.last(); c.Method != "SetSessionMode" || c.Params["mode"] != "hijack" {
		t.Fatalf("wrong SetSessionMode: %#v", c)
	}
	invoke(t, findTool(t, tools, "worker_input_mode"), map[string]any{"worker_id": "w1", "mode": "open"})
	if c := f.last(); c.Method != "SetInputMode" || c.Params["mode"] != "open" {
		t.Fatalf("wrong SetInputMode: %#v", c)
	}
	invoke(t, findTool(t, tools, "worker_disconnect"), map[string]any{"worker_id": "w1"})
	if f.last().Method != "DisconnectWorker" {
		t.Fatalf("expected DisconnectWorker, got %q", f.last().Method)
	}
}

func TestSessionListAndStatus(t *testing.T) {
	f := &fakeClient{anyResp: []any{map[string]any{"id": "s1"}}, objResp: map[string]any{"id": "s1"}}
	tools := sessionTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "session_list"), map[string]any{})
	if res["success"] != true || !reflect.DeepEqual(res["data"], []any{map[string]any{"id": "s1"}}) {
		t.Fatalf("session_list should wrap the array under data: %#v", res)
	}
	invoke(t, findTool(t, tools, "session_status"), map[string]any{"session_id": "s1"})
	if f.last().Method != "GetSession" {
		t.Fatalf("expected GetSession, got %q", f.last().Method)
	}
}

func TestSessionReadCleansSnapshot(t *testing.T) {
	f := &fakeClient{anyResp: map[string]any{"snapshot": map[string]any{"screen": "\x1b[1mX\x1b[0m", "cols": 80}}}
	tools := sessionTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "session_read"), map[string]any{"session_id": "s1", "output": "rendered"})
	snap := res["snapshot"].(map[string]any)
	if snap["screen"] != "X" || snap["cols"] != 80 {
		t.Fatalf("rendered snapshot wrong: %#v", snap)
	}
}

func TestSessionConnectDisconnect(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "session_connect"), map[string]any{"session_id": "s1"})
	if f.last().Method != "ConnectSession" {
		t.Fatalf("expected ConnectSession, got %q", f.last().Method)
	}
	invoke(t, findTool(t, tools, "session_disconnect"), map[string]any{"session_id": "s1"})
	if f.last().Method != "DisconnectSession" {
		t.Fatalf("expected DisconnectSession, got %q", f.last().Method)
	}
}

func TestSessionCreateDispatch(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "session_create"), map[string]any{
		"connector_type": "telnet", "host": "example.com", "port": 23, "display_name": "d",
		"username": "u", "password": "p", "input_mode": "open",
	})
	call := f.last()
	if call.Method != "QuickConnect" || call.Params["connector_type"] != "telnet" || call.Params["display_name"] != "d" {
		t.Fatalf("wrong QuickConnect: %#v", call)
	}
	cfg := call.Params["config"].(map[string]any)
	if cfg["host"] != "example.com" || cfg["port"] != 23 || cfg["username"] != "u" || cfg["password"] != "p" || cfg["input_mode"] != "open" {
		t.Fatalf("wrong connector config: %#v", cfg)
	}
}

func TestSessionCreateRejections(t *testing.T) {
	f, tools := sessionFixture()
	bad := invoke(t, findTool(t, tools, "session_create"), map[string]any{"connector_type": "rce"})
	if bad["error"] != "invalid_connector_type" || bad["connector_type"] != "rce" {
		t.Fatalf("expected invalid_connector_type, got %#v", bad)
	}
	ssrf := invoke(t, findTool(t, tools, "session_create"), map[string]any{"connector_type": "ssh", "host": "169.254.169.254"})
	if ssrf["error"] != "invalid_host" || ssrf["host"] != "169.254.169.254" {
		t.Fatalf("expected invalid_host, got %#v", ssrf)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no RPC should fire on a rejected session_create: %#v", f.calls)
	}
}

func TestSessionWatchClampsAndDispatches(t *testing.T) {
	f := &fakeClient{anyResp: map[string]any{"events": []any{}}}
	tools := sessionTools(f, adminAuth())
	invoke(t, findTool(t, tools, "session_watch"), map[string]any{
		"session_id": "s1", "timeout_s": 999.0, "max_events": 999, "event_types": "snapshot",
	})
	call := f.last()
	if call.Method != "WatchSessionEvents" {
		t.Fatalf("expected WatchSessionEvents, got %q", call.Method)
	}
	if call.Params["timeout_ms"] != 30000 || call.Params["max_events"] != 50 {
		t.Fatalf("watch clamps wrong: %#v", call.Params)
	}
	if call.Params["event_types"] != "snapshot" {
		t.Fatalf("event_types not forwarded: %#v", call.Params)
	}
}

func TestSessionSubscribeMatchedPattern(t *testing.T) {
	f := &fakeClient{anyResp: map[string]any{"events": []any{
		map[string]any{"data": map[string]any{"screen": "login: "}},
		map[string]any{"data": map[string]any{"screen": "$ ready"}},
	}}}
	tools := sessionTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "session_subscribe"), map[string]any{
		"session_id": "s1", "pattern": `\$ ready`, "duration_s": 0.5, "max_events": 9999,
	})
	if res["matched_pattern"] != true {
		t.Fatalf("expected matched_pattern true, got %#v", res["matched_pattern"])
	}
	call := f.last()
	if call.Params["timeout_ms"] != 1000 || call.Params["max_events"] != 500 {
		t.Fatalf("subscribe clamps wrong: %#v", call.Params)
	}
}

func TestSessionSubscribeNoMatch(t *testing.T) {
	f := &fakeClient{anyResp: map[string]any{"events": []any{
		map[string]any{"data": map[string]any{"screen": "nothing"}},
	}}}
	tools := sessionTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "session_subscribe"), map[string]any{"session_id": "s1", "pattern": "zzz"})
	if res["matched_pattern"] != false {
		t.Fatalf("expected matched_pattern false, got %#v", res["matched_pattern"])
	}
}

func TestSessionSubscribeRejectsBadPattern(t *testing.T) {
	f, tools := sessionFixture()
	res := invoke(t, findTool(t, tools, "session_subscribe"), map[string]any{"session_id": "s1", "pattern": "(a*)*"})
	if res["error"] != "invalid_pattern" {
		t.Fatalf("expected invalid_pattern, got %#v", res)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no RPC after pattern rejection")
	}
}

func TestFanoutTools(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "fanout_group_create"), map[string]any{
		"session_ids": []any{"s1", "s2"}, "name": "fleet", "mode": "parallel",
	})
	call := f.last()
	if call.Params["path"] != "/api/fanout/groups" {
		t.Fatalf("wrong fanout group path: %#v", call.Params)
	}
	body := call.Params["body"].(map[string]any)
	if !reflect.DeepEqual(body["worker_ids"], []string{"s1", "s2"}) || body["name"] != "fleet" {
		t.Fatalf("wrong fanout group body: %#v", body)
	}

	invoke(t, findTool(t, tools, "fanout_send"), map[string]any{"group_id": "g1", "data": "ls\n"})
	send := f.last()
	if send.Params["path"] != "/api/fanout/groups/g1/send" {
		t.Fatalf("wrong fanout send path: %#v", send.Params)
	}
	sbody := send.Params["body"].(map[string]any)
	if sbody["data"] != "ls\n" || sbody["quiesce_ms"] != 500 || sbody["max_response_ms"] != 10000 {
		t.Fatalf("wrong fanout send body: %#v", sbody)
	}
}

func TestFanoutSendBadGroupID(t *testing.T) {
	f, tools := sessionFixture()
	res := invoke(t, findTool(t, tools, "fanout_send"), map[string]any{"group_id": "..", "data": "x"})
	if res["error"] != "invalid_id" {
		t.Fatalf("expected invalid_id, got %#v", res)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no RPC after id rejection")
	}
}

func TestSessionAnnotateDispatch(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "session_annotate"), map[string]any{
		"session_id": "s1", "label": "boom", "description": "d", "severity": "warn",
	})
	call := f.last()
	if call.Params["path"] != "/api/sessions/s1/annotate" {
		t.Fatalf("wrong annotate path: %#v", call.Params)
	}
	body := call.Params["body"].(map[string]any)
	if body["label"] != "boom" || body["description"] != "d" || body["severity"] != "warn" {
		t.Fatalf("wrong annotate body: %#v", body)
	}
}

func TestSessionAnnotateDefaults(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "session_annotate"), map[string]any{"session_id": "s1", "label": "x"})
	body := f.last().Params["body"].(map[string]any)
	if body["description"] != "" || body["severity"] != "info" {
		t.Fatalf("expected default description/severity, got %#v", body)
	}
}

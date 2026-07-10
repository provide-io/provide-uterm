//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"
	"errors"
	"reflect"
	"testing"

	mcpgo "github.com/mark3labs/mcp-go/mcp"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

func TestRoleOrdering(t *testing.T) {
	if !roleAtLeast("admin", "operator") || !roleAtLeast("operator", "viewer") || !roleAtLeast("viewer", "viewer") {
		t.Fatalf("role ladder broken")
	}
	if roleAtLeast("viewer", "admin") || roleAtLeast("nonsense", "viewer") {
		t.Fatalf("under-privileged roles must not satisfy higher minimums")
	}
}

func TestRequiredRoleCoversEveryTool(t *testing.T) {
	for _, name := range AllToolNames {
		if _, ok := requiredRole(name); !ok {
			t.Errorf("tool %q has no policy entry", name)
		}
	}
	if len(toolRequiredRoles) != len(AllToolNames) {
		t.Fatalf("policy table (%d) and tool list (%d) diverge", len(toolRequiredRoles), len(AllToolNames))
	}
	if _, ok := requiredRole("does_not_exist"); ok {
		t.Fatalf("unknown tool must not resolve a role")
	}
}

func TestIsAllowedConnector(t *testing.T) {
	for _, c := range []string{"shell", "telnet", "ssh", "ws", "websocket", "pty"} {
		if !isAllowedConnector(c) {
			t.Errorf("%q should be allowed", c)
		}
	}
	if isAllowedConnector("rce") || isAllowedConnector("") {
		t.Fatalf("disallowed connector accepted")
	}
}

func TestPrincipalFromHeaders(t *testing.T) {
	if principalFromHeaders(nil) != nil || principalFromHeaders(map[string]string{"x": "y"}) != nil {
		t.Fatalf("absent identity headers -> nil principal")
	}
	p := principalFromHeaders(map[string]string{"X-Uterm-Principal": "svc", "x-uterm-role": "admin"})
	if p == nil || p.SubjectID != "svc" || !reflect.DeepEqual(p.Roles, []string{"admin"}) {
		t.Fatalf("header principal wrong: %#v", p)
	}
	// Role only, no subject -> anonymous subject.
	p = principalFromHeaders(map[string]string{"X-Uterm-Role": "viewer"})
	if p.SubjectID != "anonymous" || !reflect.DeepEqual(p.Roles, []string{"viewer"}) {
		t.Fatalf("role-only principal wrong: %#v", p)
	}
	// Subject only -> default viewer role.
	p = principalFromHeaders(map[string]string{"X-Uterm-Principal": "svc"})
	if !reflect.DeepEqual(p.Roles, []string{"viewer"}) {
		t.Fatalf("subject-only principal should default to viewer: %#v", p)
	}
}

func TestResolvePrincipal(t *testing.T) {
	def := newPrincipal("d", "viewer")
	if got := resolvePrincipal(context.Background(), def); got.SubjectID != "d" {
		t.Fatalf("missing ctx principal should fall back to default")
	}
	ctx := WithPrincipal(context.Background(), newPrincipal("req", "admin"))
	if got := resolvePrincipal(ctx, def); got.SubjectID != "req" {
		t.Fatalf("ctx principal should win")
	}
}

func TestGuardDeniesUnderPrivileged(t *testing.T) {
	auth := &AuthorizationContext{DefaultPrincipal: newPrincipal("v", "viewer")}
	f := &fakeClient{objResp: map[string]any{"ok": true}}
	tools := hijackTools(f, auth) // hijack_begin requires admin
	res := invoke(t, findTool(t, tools, "hijack_begin"), map[string]any{"worker_id": "w1"})
	if res["error"] != "authorization_denied" || res["required_role"] != "admin" || res["principal"] != "v" {
		t.Fatalf("expected denial payload, got %#v", res)
	}
	if !reflect.DeepEqual(res["principal_roles"], []string{"viewer"}) {
		t.Fatalf("principal_roles wrong: %#v", res["principal_roles"])
	}
	if len(f.calls) != 0 {
		t.Fatalf("denied tool must not reach the client")
	}
}

func TestGuardAllowsSufficientRole(t *testing.T) {
	// viewer may call a viewer-tier tool.
	auth := &AuthorizationContext{DefaultPrincipal: newPrincipal("v", "viewer")}
	f := &fakeClient{objResp: map[string]any{"status": "ok"}}
	tools := hijackTools(f, auth)
	res := invoke(t, findTool(t, tools, "server_health"), map[string]any{})
	if res["success"] != true {
		t.Fatalf("viewer should be allowed server_health: %#v", res)
	}
}

func TestGuardUnknownToolPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatalf("guard for an unpoliced tool must panic")
		}
	}()
	auth := &AuthorizationContext{DefaultPrincipal: newPrincipal("x", "admin")}
	_ = auth.guard("no_such_tool", func(context.Context, mcpgo.CallToolRequest) map[string]any { return nil })
}

func TestOkAny(t *testing.T) {
	merged := okAny(true, map[string]any{"a": 1})
	if merged["success"] != true || merged["a"] != 1 {
		t.Fatalf("map payload should merge: %#v", merged)
	}
	// Payload key wins over the injected success flag.
	override := okAny(false, map[string]any{"success": "kept"})
	if override["success"] != "kept" {
		t.Fatalf("payload success should win: %#v", override)
	}
	wrapped := okAny(false, []any{1, 2})
	if wrapped["success"] != false || !reflect.DeepEqual(wrapped["data"], []any{1, 2}) {
		t.Fatalf("non-map payload should be wrapped under data: %#v", wrapped)
	}
}

func TestBodyOf(t *testing.T) {
	apiErr := &client.APIError{StatusCode: 404, Body: map[string]any{"error": "not found"}}
	if got := bodyOf(apiErr); !reflect.DeepEqual(got, map[string]any{"error": "not found"}) {
		t.Fatalf("APIError body not surfaced: %#v", got)
	}
	plain := bodyOf(errors.New("boom")).(map[string]any)
	if plain["error"] != "boom" {
		t.Fatalf("plain error should wrap: %#v", plain)
	}
}

func TestResultFromError(t *testing.T) {
	apiErr := &client.APIError{StatusCode: 409, Body: map[string]any{"error": "conflict", "detail": "x"}}
	res := resultFromObject(nil, apiErr)
	if res["success"] != false || res["error"] != "conflict" || res["detail"] != "x" {
		t.Fatalf("error result should merge body: %#v", res)
	}
	resAny := resultFromAny(nil, apiErr)
	if resAny["success"] != false || resAny["error"] != "conflict" {
		t.Fatalf("resultFromAny error wrong: %#v", resAny)
	}
}

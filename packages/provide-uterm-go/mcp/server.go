//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"
	"fmt"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// serverName / serverVersion identify the MCP server to clients, matching the
// Python FastMCP("uterm") name.
const (
	serverName    = "uterm"
	serverVersion = "0.1.0"
)

// UtermClient is the subset of the ported REST client the MCP tools call. The
// concrete *client.HijackClient satisfies it; tests inject a fake to assert the
// exact REST/WS call each tool makes.
type UtermClient interface {
	Acquire(ctx context.Context, workerID string, opts client.AcquireOptions) (map[string]any, error)
	Heartbeat(ctx context.Context, workerID, hijackID string, leaseS int) (map[string]any, error)
	Snapshot(ctx context.Context, workerID, hijackID string, waitMS int) (map[string]any, error)
	Events(ctx context.Context, workerID, hijackID string, opts client.EventsOptions) (map[string]any, error)
	Send(ctx context.Context, workerID, hijackID string, opts client.SendOptions) (map[string]any, error)
	Step(ctx context.Context, workerID, hijackID string) (map[string]any, error)
	Release(ctx context.Context, workerID, hijackID string) (map[string]any, error)
	Health(ctx context.Context) (map[string]any, error)
	SetSessionMode(ctx context.Context, sessionID, mode string) (map[string]any, error)
	SetInputMode(ctx context.Context, workerID, mode string) (map[string]any, error)
	DisconnectWorker(ctx context.Context, workerID string) (map[string]any, error)
	ListSessions(ctx context.Context) (any, error)
	GetSession(ctx context.Context, sessionID string) (map[string]any, error)
	SessionSnapshot(ctx context.Context, sessionID string) (any, error)
	ConnectSession(ctx context.Context, sessionID string) (map[string]any, error)
	DisconnectSession(ctx context.Context, sessionID string) (map[string]any, error)
	QuickConnect(ctx context.Context, connectorType string, opts client.QuickConnectOptions) (map[string]any, error)
	WatchSessionEvents(ctx context.Context, sessionID string, opts client.WatchOptions) (any, error)
	Post(ctx context.Context, path string, body map[string]any) (any, error)
}

// Compile-time assertion that the real client satisfies the tool interface.
var _ UtermClient = (*client.HijackClient)(nil)

// toolFunc is a tool body returning the _ok-shaped result dict; guard() adapts
// it to the mcp-go handler signature and applies the authorization chokepoint.
type toolFunc func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any

// guard wraps a tool body with the authorization chokepoint. It resolves the
// per-request principal (falling back to the server default) and returns a
// structured authorization_denied result when the principal is under-privileged.
// A tool with no registered policy entry panics at registration time — an
// unguarded tool must never slip through.
func (a *AuthorizationContext) guard(tool string, fn toolFunc) server.ToolHandlerFunc {
	minimum, ok := requiredRole(tool)
	if !ok {
		panic(fmt.Sprintf("mcp: no authorization policy registered for MCP tool %q", tool))
	}
	return func(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
		principal := resolvePrincipal(ctx, a.DefaultPrincipal)
		if !principal.hasAtLeast(minimum) {
			return toolResult(denyPayload(tool, minimum, principal))
		}
		return toolResult(fn(ctx, req))
	}
}

// Config configures New. Mirrors the parameters of the Python create_mcp_app.
type Config struct {
	// BaseURL is the root URL of the provide-uterm server (required).
	BaseURL string
	// EntityPrefix is the worker path prefix (default "/worker").
	EntityPrefix string
	// Headers are extra HTTP headers sent with every request (e.g. auth tokens).
	Headers map[string]string
	// DefaultRole is the role granted to the stdio caller when no identity
	// headers/principal are supplied. One of "admin", "operator", "viewer"
	// (default "operator").
	DefaultRole string
	// DefaultPrincipal, when set, overrides header/role inference.
	DefaultPrincipal *McpPrincipal
}

// New builds an MCP server with all provide-uterm tools, pre-bound to a REST
// client rooted at cfg.BaseURL. Port of create_mcp_app: it validates the
// default role, constructs the client, and derives the fallback principal from
// the auth headers or the default role (stdio/local development defaults to
// operator, never admin — operators must opt in to admin explicitly).
func New(cfg Config) (*server.MCPServer, error) {
	role := cfg.DefaultRole
	if role == "" {
		role = "operator"
	}
	switch role {
	case "admin", "operator", "viewer":
	default:
		return nil, fmt.Errorf("DefaultRole must be one of 'admin', 'operator', 'viewer'; got %q", role)
	}

	opts := []client.Option{}
	if cfg.EntityPrefix != "" {
		opts = append(opts, client.WithEntityPrefix(cfg.EntityPrefix))
	}
	if len(cfg.Headers) > 0 {
		opts = append(opts, client.WithHeaders(cfg.Headers))
	}
	c := client.NewHijackClient(cfg.BaseURL, opts...)

	principal := newPrincipal("local", role)
	switch {
	case cfg.DefaultPrincipal != nil:
		principal = *cfg.DefaultPrincipal
	default:
		if fromHeaders := principalFromHeaders(cfg.Headers); fromHeaders != nil {
			principal = *fromHeaders
		}
	}
	return NewServer(c, &AuthorizationContext{DefaultPrincipal: principal}), nil
}

// NewServer wires every provide-uterm tool onto a fresh *server.MCPServer using
// the injected client and authorization context. Exposed for tests that need a
// fake client.
func NewServer(c UtermClient, authCtx *AuthorizationContext) *server.MCPServer {
	s := server.NewMCPServer(serverName, serverVersion, server.WithToolCapabilities(false))
	s.AddTools(hijackTools(c, authCtx)...)
	s.AddTools(sessionTools(c, authCtx)...)
	return s
}

// AllToolNames lists every registered MCP tool name (the 21-tool surface),
// grouped hijack/server-control then session/watch/fanout/annotate. Exposed for
// introspection and the parity test.
var AllToolNames = []string{
	// hijack lifecycle
	"hijack_begin", "hijack_heartbeat", "hijack_read", "hijack_send",
	"hijack_step", "hijack_release",
	// server / worker control
	"server_health", "session_set_mode", "worker_input_mode", "worker_disconnect",
	// session management
	"session_list", "session_status", "session_read", "session_connect",
	"session_disconnect", "session_create",
	// real-time event subscription
	"session_watch", "session_subscribe",
	// fan-out
	"fanout_group_create", "fanout_send",
	// annotation
	"session_annotate",
}

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

// sessionTools registers session management, real-time event subscription,
// fan-out, and annotation tools. Port of provide.uterm.ai.server_tools_session.

func sessionTools(c UtermClient, auth *AuthorizationContext) []server.ServerTool {
	return []server.ServerTool{
		sessionListTool(c, auth),
		sessionStatusTool(c, auth),
		sessionReadTool(c, auth),
		sessionConnectTool(c, auth),
		sessionDisconnectTool(c, auth),
		sessionCreateTool(c, auth),
		sessionWatchTool(c, auth),
		sessionSubscribeTool(c, auth),
		fanoutGroupCreateTool(c, auth),
		fanoutSendTool(c, auth),
		sessionAnnotateTool(c, auth),
	}
}

// clampInt clamps v to [lo, hi].
func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// clampFloat clamps v to [lo, hi].
func clampFloat(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func sessionListTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_list", mcpgo.WithDescription("List all sessions with status."))
	fn := func(ctx context.Context, _ mcpgo.CallToolRequest) map[string]any {
		v, err := c.ListSessions(ctx)
		return resultFromAny(v, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_list", fn)}
}

func sessionStatusTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_status",
		mcpgo.WithDescription("Get a single session's details."),
		mcpgo.WithString("session_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		m, err := c.GetSession(ctx, sessionID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_status", fn)}
}

func sessionReadTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_read",
		mcpgo.WithDescription("Get terminal snapshot for a session."),
		mcpgo.WithString("session_id", mcpgo.Required()),
		mcpgo.WithString("output", mcpgo.DefaultString("text"), mcpgo.Enum("text", "rendered", "raw"),
			mcpgo.Description("'text' (ANSI stripped), 'rendered' (grid + layout), or 'raw'.")),
		mcpgo.WithNumber("tail_lines", mcpgo.Description("Trim the screen text to the last N lines.")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		v, err := c.SessionSnapshot(ctx, sessionID)
		result := resultFromAny(v, err)
		if err == nil {
			if snap, ok := result["snapshot"].(map[string]any); ok && len(snap) > 0 {
				result["snapshot"] = cleanSnapshot(snap, req.GetString("output", "text"), optInt(req, "tail_lines"))
			}
		}
		return result
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_read", fn)}
}

func sessionConnectTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_connect",
		mcpgo.WithDescription("Start/connect a session."),
		mcpgo.WithString("session_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		m, err := c.ConnectSession(ctx, sessionID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_connect", fn)}
}

func sessionDisconnectTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_disconnect",
		mcpgo.WithDescription("Stop/disconnect a session."),
		mcpgo.WithString("session_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		m, err := c.DisconnectSession(ctx, sessionID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_disconnect", fn)}
}

func sessionCreateTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_create",
		mcpgo.WithDescription("Create an ephemeral session via quick-connect."),
		mcpgo.WithString("connector_type", mcpgo.Required(),
			mcpgo.Enum("shell", "telnet", "ssh", "ws", "websocket", "pty")),
		mcpgo.WithString("display_name"),
		mcpgo.WithString("host"),
		mcpgo.WithNumber("port"),
		mcpgo.WithString("url"),
		mcpgo.WithString("username"),
		mcpgo.WithString("password"),
		mcpgo.WithString("input_mode"),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		connectorType := req.GetString("connector_type", "")
		host := optString(req, "host")
		urlArg := optString(req, "url")
		port := optInt(req, "port")
		if r := validateSessionCreateConfig(connectorType, urlArg, port, host); r != nil {
			return r
		}
		config := map[string]any{}
		if host != nil {
			config["host"] = *host
		}
		if port != nil {
			config["port"] = *port
		}
		if urlArg != nil {
			config["url"] = *urlArg
		}
		if username := optString(req, "username"); username != nil {
			config["username"] = *username
		}
		if password := optString(req, "password"); password != nil {
			config["password"] = *password
		}
		if inputMode := optString(req, "input_mode"); inputMode != nil {
			config["input_mode"] = *inputMode
		}
		m, err := c.QuickConnect(ctx, connectorType, client.QuickConnectOptions{
			DisplayName: deref(optString(req, "display_name")),
			Config:      config,
		})
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_create", fn)}
}

func sessionWatchTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_watch",
		mcpgo.WithDescription("Watch a session for events in real time."),
		mcpgo.WithString("session_id", mcpgo.Required()),
		mcpgo.WithString("event_types", mcpgo.Description("Comma-separated event types to filter on.")),
		mcpgo.WithString("pattern", mcpgo.Description("Regex applied to snapshot event screen text.")),
		mcpgo.WithNumber("timeout_s", mcpgo.DefaultNumber(10.0), mcpgo.Description("Wait time before returning (clamped to 30s).")),
		mcpgo.WithNumber("max_events", mcpgo.DefaultNumber(50), mcpgo.Description("Max events to collect (clamped 1-50).")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		pattern := optString(req, "pattern")
		if r := rejectBadPattern(pattern); r != nil {
			return r
		}
		timeoutMS := int(clampFloat(req.GetFloat("timeout_s", 10.0), 0.1, 30) * 1000)
		v, err := c.WatchSessionEvents(ctx, sessionID, client.WatchOptions{
			EventTypes: deref(optString(req, "event_types")),
			Pattern:    deref(pattern),
			TimeoutMS:  timeoutMS,
			MaxEvents:  clampInt(req.GetInt("max_events", 50), 1, 50),
		})
		return resultFromAny(v, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_watch", fn)}
}

func sessionSubscribeTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_subscribe",
		mcpgo.WithDescription("Long-running session subscription for agent loops."),
		mcpgo.WithString("session_id", mcpgo.Required()),
		mcpgo.WithString("event_types", mcpgo.Description("Comma-separated event types to filter on.")),
		mcpgo.WithString("pattern", mcpgo.Description("Regex applied to snapshot event screen text.")),
		mcpgo.WithNumber("duration_s", mcpgo.DefaultNumber(30.0), mcpgo.Description("Subscription window (clamped 1-120s).")),
		mcpgo.WithNumber("max_events", mcpgo.DefaultNumber(200), mcpgo.Description("Max events to collect (clamped 1-500).")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		pattern := optString(req, "pattern")
		compiled, rejection := compiledPatternOrRejection(pattern)
		if rejection != nil {
			return rejection
		}
		durationMS := int(clampFloat(req.GetFloat("duration_s", 30.0), 1.0, 120.0) * 1000)
		v, err := c.WatchSessionEvents(ctx, sessionID, client.WatchOptions{
			EventTypes: deref(optString(req, "event_types")),
			Pattern:    deref(pattern),
			TimeoutMS:  durationMS,
			MaxEvents:  clampInt(req.GetInt("max_events", 200), 1, 500),
		})
		result := resultFromAny(v, err)
		matched := false
		if compiled != nil && err == nil {
			if events, ok := result["events"].([]any); ok {
				for _, ev := range events {
					evMap, isMap := ev.(map[string]any)
					if !isMap {
						continue
					}
					if compiled.MatchString(eventScreen(evMap)) {
						matched = true
						break
					}
				}
			}
		}
		result["matched_pattern"] = matched
		return result
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_subscribe", fn)}
}

// eventScreen extracts the screen text from an event's "data" payload, matching
// the Python enrichment loop (payload.get("screen", ""), stringified).
func eventScreen(ev map[string]any) string {
	payload, ok := ev["data"].(map[string]any)
	if !ok {
		return ""
	}
	switch s := payload["screen"].(type) {
	case nil:
		return ""
	case string:
		return s
	default:
		return fmt.Sprint(s)
	}
}

func fanoutGroupCreateTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("fanout_group_create",
		mcpgo.WithDescription("Create a fan-out group to broadcast input to multiple sessions simultaneously."),
		mcpgo.WithArray("session_ids", mcpgo.Required(), mcpgo.WithStringItems()),
		mcpgo.WithString("name", mcpgo.DefaultString("fleet")),
		mcpgo.WithString("mode", mcpgo.DefaultString("parallel")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionIDs := req.GetStringSlice("session_ids", nil)
		v, err := c.Post(ctx, "/api/fanout/groups", map[string]any{
			"name":       req.GetString("name", "fleet"),
			"worker_ids": sessionIDs,
			"mode":       req.GetString("mode", "parallel"),
		})
		return resultFromAny(v, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("fanout_group_create", fn)}
}

func fanoutSendTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("fanout_send",
		mcpgo.WithDescription("Broadcast input to all sessions in a fan-out group and return per-session results with divergence detection."),
		mcpgo.WithString("group_id", mcpgo.Required()),
		mcpgo.WithString("data", mcpgo.Required()),
		mcpgo.WithNumber("quiesce_ms", mcpgo.DefaultNumber(500)),
		mcpgo.WithNumber("max_response_ms", mcpgo.DefaultNumber(10000)),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		groupID := req.GetString("group_id", "")
		if r := rejectBadID(groupID, "group_id"); r != nil {
			return r
		}
		v, err := c.Post(ctx, "/api/fanout/groups/"+groupID+"/send", map[string]any{
			"data":            req.GetString("data", ""),
			"quiesce_ms":      req.GetInt("quiesce_ms", 500),
			"max_response_ms": req.GetInt("max_response_ms", 10000),
		})
		return resultFromAny(v, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("fanout_send", fn)}
}

func sessionAnnotateTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_annotate",
		mcpgo.WithDescription("Add an annotation to a session's recording timeline. Use this to mark important moments."),
		mcpgo.WithString("session_id", mcpgo.Required()),
		mcpgo.WithString("label", mcpgo.Required()),
		mcpgo.WithString("description", mcpgo.DefaultString("")),
		mcpgo.WithString("severity", mcpgo.DefaultString("info")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		v, err := c.Post(ctx, "/api/sessions/"+sessionID+"/annotate", map[string]any{
			"label":       req.GetString("label", ""),
			"description": req.GetString("description", ""),
			"severity":    req.GetString("severity", "info"),
		})
		return resultFromAny(v, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_annotate", fn)}
}

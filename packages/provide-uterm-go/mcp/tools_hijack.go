//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"context"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// hijackTools registers the six hijack-lease tools plus the four server/worker
// control tools. Port of provide.uterm.ai.server_tools_hijack.

func hijackTools(c UtermClient, auth *AuthorizationContext) []server.ServerTool {
	return []server.ServerTool{
		hijackBeginTool(c, auth),
		hijackHeartbeatTool(c, auth),
		hijackReadTool(c, auth),
		hijackSendTool(c, auth),
		hijackStepTool(c, auth),
		hijackReleaseTool(c, auth),
		serverHealthTool(c, auth),
		sessionSetModeTool(c, auth),
		workerInputModeTool(c, auth),
		workerDisconnectTool(c, auth),
	}
}

func hijackBeginTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_begin",
		mcpgo.WithDescription("Acquire a lease-based hijack session for a running worker."),
		mcpgo.WithString("worker_id", mcpgo.Required(), mcpgo.Description("Worker to hijack.")),
		mcpgo.WithNumber("lease_s", mcpgo.DefaultNumber(90), mcpgo.Description("Lease duration in seconds.")),
		mcpgo.WithString("owner", mcpgo.DefaultString("operator"), mcpgo.Description("Lease owner identity.")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		if r := rejectBadID(workerID, "worker_id"); r != nil {
			return r
		}
		m, err := c.Acquire(ctx, workerID, client.AcquireOptions{
			Owner:  req.GetString("owner", "operator"),
			LeaseS: req.GetInt("lease_s", 90),
		})
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_begin", fn)}
}

func hijackHeartbeatTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_heartbeat",
		mcpgo.WithDescription("Extend a hijack lease."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithNumber("lease_s", mcpgo.DefaultNumber(90)),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		if r := rejectBadIDs(idPair{workerID, "worker_id"}, idPair{hijackID, "hijack_id"}); r != nil {
			return r
		}
		m, err := c.Heartbeat(ctx, workerID, hijackID, req.GetInt("lease_s", 90))
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_heartbeat", fn)}
}

func hijackReadTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_read",
		mcpgo.WithDescription("Read snapshot or events from an active hijack session."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithString("mode", mcpgo.DefaultString("snapshot"), mcpgo.Enum("snapshot", "events"),
			mcpgo.Description("'snapshot' for current terminal state, 'events' for the event log.")),
		mcpgo.WithString("output", mcpgo.DefaultString("text"), mcpgo.Enum("text", "rendered", "raw"),
			mcpgo.Description("'text' (ANSI stripped), 'rendered' (grid + layout), or 'raw'.")),
		mcpgo.WithNumber("wait_ms", mcpgo.DefaultNumber(1500), mcpgo.Description("Snapshot polling timeout.")),
		mcpgo.WithNumber("after_seq", mcpgo.DefaultNumber(0), mcpgo.Description("Return events after this sequence.")),
		mcpgo.WithNumber("limit", mcpgo.DefaultNumber(200), mcpgo.Description("Max events to return.")),
		mcpgo.WithNumber("tail_lines", mcpgo.Description("Trim the screen text to the last N lines.")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		if r := rejectBadIDs(idPair{workerID, "worker_id"}, idPair{hijackID, "hijack_id"}); r != nil {
			return r
		}
		mode := req.GetString("mode", "snapshot")
		output := req.GetString("output", "text")
		var (
			m   map[string]any
			err error
		)
		if mode == "events" {
			m, err = c.Events(ctx, workerID, hijackID, client.EventsOptions{
				AfterSeq: req.GetInt("after_seq", 0),
				Limit:    req.GetInt("limit", 200),
			})
		} else {
			m, err = c.Snapshot(ctx, workerID, hijackID, req.GetInt("wait_ms", 1500))
		}
		result := resultFromObject(m, err)
		if err == nil && mode != "events" {
			if snap, ok := result["snapshot"].(map[string]any); ok && len(snap) > 0 {
				result["snapshot"] = cleanSnapshot(snap, output, optInt(req, "tail_lines"))
			}
		}
		return result
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_read", fn)}
}

func hijackSendTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_send",
		mcpgo.WithDescription("Send input to a hijacked worker, optionally guarded by prompt/regex."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithString("keys", mcpgo.Required(), mcpgo.Description("Keystrokes to send (escape sequences supported).")),
		mcpgo.WithString("expect_prompt_id", mcpgo.Description("Only send when this prompt id is on screen.")),
		mcpgo.WithString("expect_regex", mcpgo.Description("Only send when this regex matches the screen.")),
		mcpgo.WithNumber("timeout_ms", mcpgo.DefaultNumber(2000)),
		mcpgo.WithNumber("poll_interval_ms", mcpgo.DefaultNumber(120)),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		if r := rejectBadIDs(idPair{workerID, "worker_id"}, idPair{hijackID, "hijack_id"}); r != nil {
			return r
		}
		expectRegex := optString(req, "expect_regex")
		if r := rejectBadPattern(expectRegex); r != nil {
			return r
		}
		m, err := c.Send(ctx, workerID, hijackID, client.SendOptions{
			Keys:           prepareKeystrokes(req.GetString("keys", ""), MaxKeystrokeBytes),
			ExpectPromptID: deref(optString(req, "expect_prompt_id")),
			ExpectRegex:    deref(expectRegex),
			TimeoutMS:      req.GetInt("timeout_ms", 2000),
			PollIntervalMS: req.GetInt("poll_interval_ms", 120),
		})
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_send", fn)}
}

func hijackStepTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_step",
		mcpgo.WithDescription("Single-step a hijacked worker loop."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		if r := rejectBadIDs(idPair{workerID, "worker_id"}, idPair{hijackID, "hijack_id"}); r != nil {
			return r
		}
		m, err := c.Step(ctx, workerID, hijackID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_step", fn)}
}

func hijackReleaseTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("hijack_release",
		mcpgo.WithDescription("Release hijack session and resume worker automation."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		if r := rejectBadIDs(idPair{workerID, "worker_id"}, idPair{hijackID, "hijack_id"}); r != nil {
			return r
		}
		m, err := c.Release(ctx, workerID, hijackID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_release", fn)}
}

func serverHealthTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("server_health",
		mcpgo.WithDescription("Health check the provide-uterm server."),
	)
	fn := func(ctx context.Context, _ mcpgo.CallToolRequest) map[string]any {
		m, err := c.Health(ctx)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("server_health", fn)}
}

func sessionSetModeTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("session_set_mode",
		mcpgo.WithDescription("Set session input mode (hijack/open)."),
		mcpgo.WithString("session_id", mcpgo.Required()),
		mcpgo.WithString("mode", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		sessionID := req.GetString("session_id", "")
		if r := rejectBadID(sessionID, "session_id"); r != nil {
			return r
		}
		m, err := c.SetSessionMode(ctx, sessionID, req.GetString("mode", ""))
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("session_set_mode", fn)}
}

func workerInputModeTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("worker_input_mode",
		mcpgo.WithDescription("Set worker input mode directly (hijack/open)."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("mode", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		if r := rejectBadID(workerID, "worker_id"); r != nil {
			return r
		}
		m, err := c.SetInputMode(ctx, workerID, req.GetString("mode", ""))
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("worker_input_mode", fn)}
}

func workerDisconnectTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("worker_disconnect",
		mcpgo.WithDescription("Disconnect a worker WebSocket."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		if r := rejectBadID(workerID, "worker_id"); r != nil {
			return r
		}
		m, err := c.DisconnectWorker(ctx, workerID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("worker_disconnect", fn)}
}

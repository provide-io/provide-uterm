//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"errors"

	mcpgo "github.com/mark3labs/mcp-go/mcp"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/client"
)

// Result normalisation. Port of provide.uterm.client.mcp_tools._ok: fold the
// client's (value, error) return into a single MCP-friendly dict carrying a
// top-level "success" flag.

// okAny normalises (ok, data) into a single dict. A map payload is merged into
// {"success": ok, ...} (payload keys win on collision, matching Python's
// {"success": ok, **data}); any other payload is wrapped as {"data": data}.
func okAny(ok bool, data any) map[string]any {
	if m, isMap := data.(map[string]any); isMap {
		out := make(map[string]any, len(m)+1)
		out["success"] = ok
		for k, v := range m {
			out[k] = v
		}
		return out
	}
	return map[string]any{"success": ok, "data": data}
}

// bodyOf extracts the decoded response body an error carries. A *client.APIError
// always carries the decoded Body (the server's error JSON, or {"error": ...}
// for a transport failure); any other error is wrapped as {"error": msg}.
func bodyOf(err error) any {
	var apiErr *client.APIError
	if errors.As(err, &apiErr) {
		return apiErr.Body
	}
	return map[string]any{"error": err.Error()}
}

// resultFromObject folds an object-endpoint (map, error) return into the _ok
// result dict.
func resultFromObject(m map[string]any, err error) map[string]any {
	if err == nil {
		return okAny(true, m)
	}
	return okAny(false, bodyOf(err))
}

// resultFromAny folds a value-endpoint (any, error) return into the _ok result
// dict (used by the list/array/generic-POST endpoints).
func resultFromAny(v any, err error) map[string]any {
	if err == nil {
		return okAny(true, v)
	}
	return okAny(false, bodyOf(err))
}

// toolResult wraps a result dict as an MCP tool result. The dict is surfaced as
// structured content with a JSON text fallback; success:false is data, not a
// protocol error, so IsError stays false (matching FastMCP returning a dict).
func toolResult(m map[string]any) (*mcpgo.CallToolResult, error) {
	return mcpgo.NewToolResultStructuredOnly(m), nil
}

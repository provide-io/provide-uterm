package mcp

import (
	"context"

	mcpgo "github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

func guiTools(c UtermClient, auth *AuthorizationContext) []server.ServerTool {
	return []server.ServerTool{
		guiHijackBeginTool(c, auth),
		guiHijackReleaseTool(c, auth),
		guiScreenshotTool(c, auth),
		guiClickTool(c, auth),
		guiTypeTool(c, auth),
		guiKeyTool(c, auth),
		guiDragTool(c, auth),
	}
}

func guiHijackBeginTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_hijack_begin",
		mcpgo.WithDescription("Acquire a graphical lease-based hijack session."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithNumber("lease_s", mcpgo.DefaultNumber(90)),
		mcpgo.WithString("owner", mcpgo.DefaultString("operator")),
	)
	fn := hijackBeginTool(c, auth).Handler
	return server.ServerTool{Tool: tool, Handler: fn}
}

func guiHijackReleaseTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_hijack_release",
		mcpgo.WithDescription("Release graphical hijack session."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
	)
	fn := hijackReleaseTool(c, auth).Handler
	return server.ServerTool{Tool: tool, Handler: fn}
}

func guiScreenshotTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_screenshot",
		mcpgo.WithDescription("Capture GUI snapshot as base64 PNG."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		m, err := c.GUIScreenshot(ctx, workerID, hijackID)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_read", fn)}
}

func guiClickTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_click",
		mcpgo.WithDescription("Send GUI click event."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithNumber("x", mcpgo.Required()),
		mcpgo.WithNumber("y", mcpgo.Required()),
		mcpgo.WithString("button", mcpgo.DefaultString("left")),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		x := int(req.GetFloat("x", 0))
		y := int(req.GetFloat("y", 0))
		btn := req.GetString("button", "left")
		m, err := c.GUIClick(ctx, workerID, hijackID, x, y, btn)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_send", fn)}
}

func guiTypeTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_type",
		mcpgo.WithDescription("Send GUI text typing event."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithString("text", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		text := req.GetString("text", "")
		m, err := c.GUIType(ctx, workerID, hijackID, text)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_send", fn)}
}

func guiKeyTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_key",
		mcpgo.WithDescription("Send GUI key event."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithString("key_name", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		keyName := req.GetString("key_name", "")
		m, err := c.GUIKey(ctx, workerID, hijackID, keyName)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_send", fn)}
}

func guiDragTool(c UtermClient, auth *AuthorizationContext) server.ServerTool {
	tool := mcpgo.NewTool("gui_drag",
		mcpgo.WithDescription("Send GUI drag event."),
		mcpgo.WithString("worker_id", mcpgo.Required()),
		mcpgo.WithString("hijack_id", mcpgo.Required()),
		mcpgo.WithNumber("start_x", mcpgo.Required()),
		mcpgo.WithNumber("start_y", mcpgo.Required()),
		mcpgo.WithNumber("end_x", mcpgo.Required()),
		mcpgo.WithNumber("end_y", mcpgo.Required()),
	)
	fn := func(ctx context.Context, req mcpgo.CallToolRequest) map[string]any {
		workerID := req.GetString("worker_id", "")
		hijackID := req.GetString("hijack_id", "")
		startX := int(req.GetFloat("start_x", 0))
		startY := int(req.GetFloat("start_y", 0))
		endX := int(req.GetFloat("end_x", 0))
		endY := int(req.GetFloat("end_y", 0))
		m, err := c.GUIDrag(ctx, workerID, hijackID, startX, startY, endX, endY)
		return resultFromObject(m, err)
	}
	return server.ServerTool{Tool: tool, Handler: auth.guard("hijack_send", fn)}
}

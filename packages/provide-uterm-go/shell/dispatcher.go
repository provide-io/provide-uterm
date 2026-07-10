//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"net/http"
	"sort"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/render"
)

// CommandDispatcher parses and dispatches ushell command lines. Port of
// commands/dispatcher.py:CommandDispatcher.
type CommandDispatcher struct {
	ctx         *Context
	client      *http.Client
	renderImage imageRenderer
}

// NewCommandDispatcher builds a dispatcher over ctx (nil is treated as an empty
// context). It uses http.DefaultClient for fetch/cast/render and the render
// package's image decoder; both are overridable in tests within the package.
func NewCommandDispatcher(ctx *Context) *CommandDispatcher {
	if ctx == nil {
		ctx = &Context{}
	}
	return &CommandDispatcher{
		ctx:         ctx,
		client:      http.DefaultClient,
		renderImage: render.ImageToANSIFrames,
	}
}

// Dispatch processes a completed line and returns the command Result. Port of
// CommandDispatcher.dispatch.
func (d *CommandDispatcher) Dispatch(ctx context.Context, line string) Result {
	line = pyStrip(line)
	// Ctrl+C — already echoed; just re-show the prompt.
	if line == "" || line == "\x03" {
		return textResult(Prompt)
	}

	parts := pySplit1(line)
	cmd := strings.ToLower(parts[0])
	arg := ""
	if len(parts) > 1 {
		arg = pyStrip(parts[1])
	}

	switch cmd {
	case "exit", "quit", "\x04":
		return textResult(InfoMsg("Goodbye.\r\n") + Prompt)

	case "help":
		if arg != "" {
			detail, found := commandHelp[strings.ToLower(arg)]
			if !found {
				return textResult(ErrorMsg("no help for '"+arg+"'") + Prompt)
			}
			return textResult(detail + Prompt)
		}
		return textResult(helpText + Prompt)

	case "clear":
		return textResult(ClearScreen + Prompt)

	case "py":
		return cmdPy(arg)

	case "sessions":
		if strings.HasPrefix(arg, "kill ") || arg == "kill" {
			id := ""
			if strings.HasPrefix(arg, "kill ") {
				id = pyStrip(arg[5:])
			}
			return cmdSessionsKill(ctx, d.ctx, id)
		}
		return cmdSessions(ctx, d.ctx)

	case "kv":
		return cmdKV(ctx, d.ctx, arg)

	case "fetch":
		return cmdFetch(ctx, d.client, arg)

	case "storage":
		return cmdStorage(ctx, d.ctx, arg)

	case "env":
		return d.cmdEnv()

	case "render":
		return cmdRender(ctx, d.client, d.renderImage, arg)

	case "cast":
		return cmdCast(ctx, d.client, arg)
	}

	return textResult(ErrorMsg("unknown command: '"+cmd+"' — type "+Bold+"help"+Reset) + Prompt)
}

// cmdEnv shows available context keys (or env attributes). Port of
// CommandDispatcher._cmd_env.
func (d *CommandDispatcher) cmdEnv() Result {
	var lines []string
	if d.ctx.Env != nil {
		attrs := d.ctx.Env.Attrs()
		names := make([]string, 0, len(attrs))
		for name := range attrs {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			lines = append(lines, FmtKVDefault(name, attrs[name]))
		}
	} else {
		keys := make([]string, 0, len(d.ctx.Values))
		for k := range d.ctx.Values {
			if !strings.HasPrefix(k, "_") {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			lines = append(lines, FmtKVDefault(k, ""))
		}
	}
	output := InfoMsg("(empty context)")
	if len(lines) > 0 {
		output = Heading("context") + strings.Join(lines, "")
	}
	return textResult(output + Prompt)
}

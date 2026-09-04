//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Command uterm-mcp runs the provide-uterm Model Context Protocol server over
// stdio. It is the Go port of the Python "uterm-mcp" CLI
// (provide.uterm.ai.cli) and exposes the same 28 tools, so an MCP client
// configuration is interchangeable between the two.
//
// Usage:
//
//	uterm-mcp --url http://localhost:8780
//	uterm-mcp --url http://localhost:8780 --entity-prefix /agent
//	uterm-mcp --url http://localhost:8780 --header 'Authorization: Bearer tok'
//	uterm-mcp --url http://localhost:8780 --role admin
package main

import (
	"fmt"
	"os"
)

func main() {
	if err := run(os.Args[1:], os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "uterm-mcp:", err)
		os.Exit(1)
	}
}

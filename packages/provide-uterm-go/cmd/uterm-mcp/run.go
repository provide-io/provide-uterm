//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package main

import (
	"flag"
	"fmt"
	"io"
	"strings"

	"github.com/mark3labs/mcp-go/server"

	umcp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/mcp"
)

// headerList is a repeatable --header flag accumulating "key: value" strings.
type headerList []string

func (h *headerList) String() string { return strings.Join(*h, ",") }

func (h *headerList) Set(v string) error {
	*h = append(*h, v)
	return nil
}

// parseConfig parses the CLI arguments into an mcp.Config. It mirrors the
// Python argparse surface: --url (required), --entity-prefix (default /worker),
// --header (repeatable "key: value"), and --role (admin|operator|viewer,
// default operator).
func parseConfig(args []string, stderr io.Writer) (umcp.Config, error) {
	fs := flag.NewFlagSet("uterm-mcp", flag.ContinueOnError)
	fs.SetOutput(stderr)

	var (
		url          = fs.String("url", "", "Base URL of the provide-uterm server (required).")
		entityPrefix = fs.String("entity-prefix", "/worker", "Path prefix for worker endpoints.")
		role         = fs.String("role", "operator", "Default role for the stdio caller (admin|operator|viewer).")
		headers      headerList
	)
	fs.Var(&headers, "header", "Extra header as 'key: value' (repeatable).")

	if err := fs.Parse(args); err != nil {
		return umcp.Config{}, err
	}
	if *url == "" {
		return umcp.Config{}, fmt.Errorf("--url is required")
	}
	switch *role {
	case "admin", "operator", "viewer":
	default:
		return umcp.Config{}, fmt.Errorf("--role must be one of admin, operator, viewer; got %q", *role)
	}

	hdrs := map[string]string{}
	for _, h := range headers {
		key, value, _ := strings.Cut(h, ":")
		key = strings.TrimSpace(key)
		if key == "" {
			continue
		}
		hdrs[key] = strings.TrimSpace(value)
	}
	if len(hdrs) == 0 {
		hdrs = nil
	}

	return umcp.Config{
		BaseURL:      *url,
		EntityPrefix: *entityPrefix,
		Headers:      hdrs,
		DefaultRole:  *role,
	}, nil
}

// run parses arguments, builds the MCP server, and serves it over stdio.
func run(args []string, stderr io.Writer) error {
	cfg, err := parseConfig(args, stderr)
	if err != nil {
		return err
	}
	srv, err := umcp.New(cfg)
	if err != nil {
		return err
	}
	return server.ServeStdio(srv)
}

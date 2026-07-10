//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/coder/websocket"
	"github.com/spf13/cobra"
)

// urlTunnelRe extracts a tunnel ID from an app/inspect/session/operator/s URL.
// Mirrors _URL_TUNNEL_RE.
var urlTunnelRe = regexp.MustCompile(`/(?:app/(?:inspect|session|operator)/|s/)([a-zA-Z0-9_-]+)`)

// newWatchCmd mirrors the Python `watch` subcommand (TUI HTTP traffic viewer).
func newWatchCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "watch TUNNEL",
		Short:        "TUI HTTP traffic viewer for tunnel sessions",
		Long:         "Connect to an existing tunnel and watch HTTP traffic in a terminal UI.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			f := cmd.Flags()
			server, _ := f.GetString("server")
			layout, _ := f.GetString("layout")
			token, _ := f.GetString("token")
			tokenFile, _ := f.GetString("token-file")
			return runWatch(cmd.Context(), args[0], server, layout, token, tokenFile)
		},
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "tunnel server URL")
	f.String("layout", "horizontal", "initial layout mode: horizontal, vertical, modal")
	f.String("token", "", "bearer token for auth")
	f.String("token-file", tokenFileDefault(), "path to token file")
	return cmd
}

// extractTunnelID extracts a tunnel ID from a bare ID or URL. Mirrors
// extract_tunnel_id.
func extractTunnelID(value string) string {
	if strings.Contains(value, "://") {
		base := strings.SplitN(value, "?", 2)[0]
		if m := urlTunnelRe.FindStringSubmatch(base); m != nil {
			return m[1]
		}
	}
	return value
}

// resolveWatchServer derives the server URL, falling back to the scheme+host of
// a URL-form tunnel argument (mirrors _cmd_watch).
func resolveWatchServer(tunnelArg, server string) (string, error) {
	if server == "" && strings.Contains(tunnelArg, "://") {
		if u, err := url.Parse(tunnelArg); err == nil {
			server = fmt.Sprintf("%s://%s", u.Scheme, u.Host)
		}
	}
	if server == "" {
		return "", fmt.Errorf("--server is required when passing a bare tunnel ID")
	}
	return server, nil
}

// watchWSURL builds the browser terminal WebSocket URL for a tunnel.
func watchWSURL(server, tunnelID string) string {
	base := strings.TrimRight(server, "/")
	base = strings.ReplaceAll(base, "http://", "ws://")
	base = strings.ReplaceAll(base, "https://", "wss://")
	return fmt.Sprintf("%s/ws/browser/%s/term", base, tunnelID)
}

// readWatchToken resolves the bearer token from --token or the token file.
func readWatchToken(token, tokenFile string) string {
	if token != "" {
		return token
	}
	if tokenFile == "" {
		return ""
	}
	data, err := os.ReadFile(tokenFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// runWatch wires the WS reader to the bubbletea program.
func runWatch(ctx context.Context, tunnelArg, server, layout, token, tokenFile string) error {
	if ctx == nil {
		ctx = context.Background()
	}
	tunnelID := extractTunnelID(tunnelArg)
	server, err := resolveWatchServer(tunnelArg, server)
	if err != nil {
		return err
	}
	wsURL := watchWSURL(server, tunnelID)
	header := http.Header{}
	if tok := readWatchToken(token, tokenFile); tok != "" {
		header.Set("Authorization", "Bearer "+tok)
	}

	model := newWatchModel(tunnelID, layout)
	program := tea.NewProgram(model, tea.WithContext(ctx), tea.WithAltScreen())
	go watchWSLoop(ctx, wsURL, header, program)
	_, err = program.Run()
	return err
}

// watchWSLoop connects to the tunnel WebSocket and streams decoded HTTP frames
// into the running bubbletea program. It reports connection-state changes and
// retries are left to the operator (the process is interactive).
func watchWSLoop(ctx context.Context, wsURL string, header http.Header, program *tea.Program) {
	conn, _, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{HTTPHeader: header})
	if err != nil {
		program.Send(connStateMsg{connected: false})
		return
	}
	conn.SetReadLimit(-1)
	defer conn.CloseNow() //nolint:errcheck // best-effort close
	program.Send(connStateMsg{connected: true})
	for {
		typ, msg, rerr := conn.Read(ctx)
		if rerr != nil {
			program.Send(connStateMsg{connected: false})
			return
		}
		if typ != websocket.MessageText {
			continue
		}
		for _, frame := range parseHTTPFrames(string(msg)) {
			program.Send(httpFrameMsg(frame))
		}
	}
}

// decodeBody renders a request/response body for the detail view. Port of
// _decode_body.
func decodeBody(b64 string, truncated, binary bool, size int) string {
	if b64 != "" {
		data, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			return "(decode error)"
		}
		return string(data)
	}
	if truncated {
		return fmt.Sprintf("(truncated, %s)", humanSize(size))
	}
	if binary {
		return fmt.Sprintf("(binary, %s)", humanSize(size))
	}
	return ""
}

// sortedKeys returns map keys in deterministic order. Deviation from the Textual
// app, which iterates headers in insertion order; Go maps are unordered, so the
// detail view sorts for stable rendering (and golden tests).
func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// tunnelInfo is the parsed subset of the POST /api/tunnels response the share,
// tunnel and inspect commands consume. Extra fields are ignored.
type tunnelInfo struct {
	TunnelID   string `json:"tunnel_id"`
	SessionID  string `json:"session_id"`
	ShareURL   string `json:"share_url"`
	ControlURL string `json:"control_url"`
	WSEndpoint string `json:"ws_endpoint"`
	WorkerTok  string `json:"worker_token"`
}

// resolvedTunnelID returns tunnel_id, falling back to session_id (matching the
// inspect.py lookup order).
func (t tunnelInfo) resolvedTunnelID() string {
	if t.TunnelID != "" {
		return t.TunnelID
	}
	return t.SessionID
}

// expandUser expands a leading "~" to the user's home directory, matching
// pathlib's expanduser. A path that cannot be expanded is returned unchanged.
func expandUser(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			if p == "~" {
				return home
			}
			return filepath.Join(home, p[2:])
		}
	}
	return p
}

// readTunnelToken resolves the bearer token from --token, else the token file
// (default ~/.uterm/session_token). A missing file yields "". Mirrors the
// _read_token helpers in share.py / tunnel.py / inspect.py.
func readTunnelToken(token, tokenFile string) string {
	if token != "" {
		return token
	}
	if tokenFile == "" {
		return ""
	}
	data, err := os.ReadFile(expandUser(tokenFile))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// createTunnel POSTs to {server}/api/tunnels and returns the parsed response.
// Errors mirror the Python messages (Execute prepends "error: ").
func createTunnel(ctx context.Context, server string, body map[string]any, token, userAgent string) (tunnelInfo, error) {
	url := strings.TrimRight(server, "/") + "/api/tunnels"
	payload, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return tunnelInfo{}, fmt.Errorf("cannot reach server: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", userAgent)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return tunnelInfo{}, fmt.Errorf("cannot reach server: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return tunnelInfo{}, fmt.Errorf("tunnel creation failed (HTTP %d): %s", resp.StatusCode, string(raw))
	}
	var info tunnelInfo
	if err := json.Unmarshal(raw, &info); err != nil {
		return tunnelInfo{}, fmt.Errorf("invalid tunnel response: %w", err)
	}
	return info, nil
}

// resolveWSEndpoint turns a possibly-relative ws_endpoint into an absolute URL
// using the server's scheme/host, matching the http→ws / https→wss rewrite the
// Python CLIs perform.
func resolveWSEndpoint(server, wsEndpoint string) string {
	if !strings.HasPrefix(wsEndpoint, "/") {
		return wsEndpoint
	}
	base := strings.TrimRight(server, "/")
	base = strings.Replace(base, "http://", "ws://", 1)
	base = strings.Replace(base, "https://", "wss://", 1)
	return base + wsEndpoint
}

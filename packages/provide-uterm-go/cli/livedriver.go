//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// LiveServerOptions configures [NewLiveServer].
type LiveServerOptions struct {
	// ConfigPath is an optional TOML config file; empty loads the defaults.
	ConfigPath string
	// AuthMode overrides the config's auth.mode (e.g. "dev_token") when set.
	AuthMode string
	// FrontendDir is the built-frontend directory; empty serves the minimal shell.
	FrontendDir string
}

// LiveServer is the `uterm server` stack bound to a listener the caller
// already opened. It exists for the cross-language live conformance harness,
// whose protocol requires every server to bind an ephemeral port and report
// what the operating system handed out — so the socket has to be opened before
// the config (and therefore public_base_url) can be resolved.
//
// It adds no wiring of its own: the server, hub, auth and control plane are
// assembled by the same buildServerFromConfig the `server` subcommand uses.
type LiveServer struct {
	bundle *serverBundle
	ln     net.Listener
}

// NewLiveServer builds a server configured for the address ln is already
// bound to. Ownership of ln transfers to the returned LiveServer.
func NewLiveServer(ctx context.Context, ln net.Listener, opts LiveServerOptions) (*LiveServer, error) {
	if ln == nil {
		return nil, errors.New("live server: nil listener")
	}
	host, port, err := listenerHostPort(ln)
	if err != nil {
		return nil, err
	}
	cfg, err := serverconfig.LoadServerConfig(opts.ConfigPath)
	if err != nil {
		return nil, err
	}
	// The listener's real address is the override, so public_base_url names the
	// port the OS chose rather than the config's default.
	applyServerOverrides(cfg, host, port)
	if mode := strings.TrimSpace(opts.AuthMode); mode != "" {
		cfg.Auth.Mode = mode
	}
	bundle, err := buildServerFromConfig(ctx, cfg, opts.FrontendDir)
	if err != nil {
		return nil, err
	}
	return &LiveServer{bundle: bundle, ln: ln}, nil
}

// listenerHostPort splits a listener's address into a host and a numeric port.
func listenerHostPort(ln net.Listener) (string, int, error) {
	host, portStr, err := net.SplitHostPort(ln.Addr().String())
	if err != nil {
		return "", 0, err
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return "", 0, err
	}
	return host, port, nil
}

// BaseURL is the http origin clients should target.
func (l *LiveServer) BaseURL() string { return "http://" + l.ln.Addr().String() }

// Token is the bearer token a client should present, or "" when the configured
// auth mode mints none (only dev_token does).
func (l *LiveServer) Token() string { return l.bundle.devToken }

// AuthMode is the auth mode the server ended up running in. Note that
// dev_token rewrites itself to "jwt" (the dev IdP mints a JWT the standard
// validator accepts), so this reports the effective mode, not the requested one.
func (l *LiveServer) AuthMode() string { return l.bundle.cfg.Auth.Mode }

// Serve runs the server on its listener until ctx is cancelled, then drains.
func (l *LiveServer) Serve(ctx context.Context) error {
	return l.bundle.srv.Serve(ctx, l.ln)
}

// Close releases the control-plane engine. Serve must have returned first.
func (l *LiveServer) Close() error {
	return l.bundle.engine.Close(context.Background())
}

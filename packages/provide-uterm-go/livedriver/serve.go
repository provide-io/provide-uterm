//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"context"
	"io"
	"net"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/cli"
)

// DefaultAuthMode is the auth mode `serve` starts in when none is asked for.
const DefaultAuthMode = "dev_token"

// ephemeralLoopback is the bind address for the server role. The "0" is not a
// port choice — it is the protocol's requirement that the operating system
// picks the port and the driver reports what it was given. No port number
// appears anywhere in this driver.
const ephemeralLoopback = "127.0.0.1:0"

// readyTimeout bounds the wait for the configured sessions to come up. On
// expiry the driver announces anyway and lets the scenario report what it
// finds — a harness that fails says more than one that hangs.
const readyTimeout = 30 * time.Second

// ServeOptions configures a server-role run.
type ServeOptions struct {
	// AuthMode is the scenario's requested auth mode; empty means dev_token.
	AuthMode string
	// ConfigPath is an optional TOML server config; empty loads the defaults.
	ConfigPath string
}

// authMode resolves the requested auth mode.
func (o ServeOptions) authMode() string {
	if mode := strings.TrimSpace(o.AuthMode); mode != "" {
		return mode
	}
	return DefaultAuthMode
}

// RunServe binds an ephemeral loopback port, starts the Go uterm server on it,
// writes the one-line handshake to stdout, and serves until stdin reaches EOF
// or ctx is cancelled (the caller wires SIGINT/SIGTERM into ctx).
func RunServe(ctx context.Context, opts ServeOptions, stdin io.Reader, stdout io.Writer) error {
	ln, err := net.Listen("tcp", ephemeralLoopback)
	if err != nil {
		return err
	}
	srv, err := cli.NewLiveServer(ctx, ln, cli.LiveServerOptions{
		ConfigPath: opts.ConfigPath,
		AuthMode:   opts.authMode(),
	})
	if err != nil {
		_ = ln.Close()
		return err
	}
	defer func() { _ = srv.Close() }()

	serveCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	if stdin != nil {
		// Closing stdin is the harness's ordinary shutdown.
		go func() {
			_, _ = io.Copy(io.Discard, stdin)
			cancel()
		}()
	}

	// Serve first, announce second. The announcement means "ready", not
	// "bound": until the configured auto_start sessions have attached
	// themselves to the hub there is a session no client can take a lease on,
	// and a driver that announced before then would be announcing something
	// the scenarios cannot rely on. The reference driver gates the same way.
	served := make(chan error, 1)
	go func() { served <- srv.Serve(serveCtx) }()

	readyCtx, readyCancel := context.WithTimeout(serveCtx, readyTimeout)
	srv.WaitReady(readyCtx)
	readyCancel()

	if err := writeHandshake(stdout, srv); err != nil {
		cancel()
		<-served
		return err
	}
	return <-served
}

// writeHandshake announces the bound server as one line of JSON.
func writeHandshake(w io.Writer, srv *cli.LiveServer) error {
	line := ServerLine{
		Role:         RoleServer,
		Language:     Language,
		BaseURL:      srv.BaseURL(),
		Token:        srv.Token(),
		Capabilities: Capabilities(),
	}
	if err := WriteLine(w, line); err != nil {
		return err
	}
	return flush(w)
}

// flush pushes a buffered writer out, so the harness sees the handshake line
// before the server blocks on Serve. An unbuffered writer (os.Stdout) is
// already there.
func flush(w io.Writer) error {
	if f, ok := w.(interface{ Flush() error }); ok {
		return f.Flush()
	}
	return nil
}

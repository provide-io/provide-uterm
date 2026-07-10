//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/coder/websocket"
	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// proxyOptions carries the resolved `uterm proxy` arguments.
type proxyOptions struct {
	Host      string // remote BBS host
	BBSPort   int    // remote BBS port
	Bind      string // local listen address
	Port      int    // local listen port
	Path      string // WebSocket endpoint path
	Transport string // "telnet" | "ssh"
}

// newProxyCmd registers the `proxy` subcommand (browser WS → remote telnet/SSH),
// mirroring the Python flags/defaults exactly.
func newProxyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "proxy HOST PORT",
		Short:        "browser WS → remote telnet/SSH (start a WS server)",
		Long:         "Accept browser WebSocket connections and proxy them to a remote telnet/SSH host.",
		Args:         cobra.ExactArgs(2),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			bbsPort, err := strconv.Atoi(args[1])
			if err != nil {
				return fmt.Errorf("PORT must be an integer: %q", args[1])
			}
			port, _ := cmd.Flags().GetInt("port")
			bind, _ := cmd.Flags().GetString("bind")
			path, _ := cmd.Flags().GetString("path")
			transport, _ := cmd.Flags().GetString("transport")
			if transport != "telnet" && transport != "ssh" {
				return fmt.Errorf("--transport must be telnet or ssh, got %q", transport)
			}
			return runProxy(cmd.Context(), proxyOptions{
				Host: args[0], BBSPort: bbsPort, Bind: bind, Port: port, Path: path, Transport: transport,
			})
		},
	}
	f := cmd.Flags()
	f.IntP("port", "p", defaults.ProxyPort, fmt.Sprintf("local HTTP listen port (default: %d)", defaults.ProxyPort))
	f.String("bind", defaults.BindAll, fmt.Sprintf("bind address (default: %s)", defaults.BindAll))
	f.String("path", defaults.ProxyWSPath, fmt.Sprintf("WebSocket endpoint path (default: %s)", defaults.ProxyWSPath))
	f.String("transport", "telnet", "outbound transport protocol: telnet or ssh (default: telnet)")
	return cmd
}

// newProxyTransport builds the outbound transport for the proxy. SSH runs with
// host-key verification disabled because the Python proxy exposes no host-key
// surface either — the proxy is intended for a trusted/local dev boundary.
func newProxyTransport(kind string) transports.ConnectionTransport {
	if kind == "ssh" {
		return transports.NewSSHTransport()
	}
	return transports.NewTelnetTransport()
}

// proxyHandler builds the WS-upgrade handler mounted at opts.Path.
func proxyHandler(opts proxyOptions) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(opts.Path, func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck // best-effort close on handler exit

		ctx, cancel := context.WithCancel(r.Context())
		defer cancel()

		tr := newProxyTransport(opts.Transport)
		connOpts := transports.ConnectOptions{}
		if opts.Transport == "ssh" {
			connOpts.SSH.InsecureSkipHostKeyVerify = true
		}
		if err := tr.Connect(ctx, opts.Host, opts.BBSPort, connOpts); err != nil {
			_ = conn.Close(websocket.StatusInternalError, "upstream connect failed")
			return
		}
		defer tr.Disconnect(context.Background()) //nolint:errcheck // best-effort

		bridgeProxy(ctx, cancel, conn, tr)
		_ = conn.Close(websocket.StatusNormalClosure, "")
	})
	return mux
}

// bridgeProxy pumps bytes both ways between a browser WebSocket and the outbound
// transport until either side closes. cancel is invoked when either pump ends so
// the other unblocks.
func bridgeProxy(ctx context.Context, cancel context.CancelFunc, conn *websocket.Conn, tr transports.ConnectionTransport) {
	var wg sync.WaitGroup
	wg.Add(2)

	// browser → remote
	go func() {
		defer wg.Done()
		defer cancel()
		for {
			_, data, err := conn.Read(ctx)
			if err != nil {
				return
			}
			if len(data) == 0 {
				continue
			}
			if err := tr.Send(ctx, data); err != nil {
				return
			}
		}
	}()

	// remote → browser
	go func() {
		defer wg.Done()
		defer cancel()
		for {
			if ctx.Err() != nil {
				return
			}
			data, err := tr.Receive(ctx, 4096, 200*time.Millisecond)
			if err != nil {
				return
			}
			if len(data) == 0 {
				continue
			}
			if err := conn.Write(ctx, websocket.MessageBinary, data); err != nil {
				return
			}
		}
	}()

	wg.Wait()
}

// serveProxy serves the proxy on an already-bound listener until ctx is
// cancelled, then drains gracefully. Exposed for tests that bind :0.
func serveProxy(ctx context.Context, ln net.Listener, opts proxyOptions) error {
	srv := &http.Server{Handler: proxyHandler(opts), ReadHeaderTimeout: 30 * time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.Serve(ln) }()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

// runProxy binds bind:port and serves the proxy until SIGINT/SIGTERM (or the
// parent context) fires, then drains gracefully.
func runProxy(ctx context.Context, opts proxyOptions) error {
	if ctx == nil {
		ctx = context.Background()
	}
	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ln, err := net.Listen("tcp", net.JoinHostPort(opts.Bind, strconv.Itoa(opts.Port)))
	if err != nil {
		return err
	}
	return serveProxy(sigCtx, ln, opts)
}

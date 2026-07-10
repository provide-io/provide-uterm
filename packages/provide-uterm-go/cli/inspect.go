//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// inspectOptions carries the resolved `uterm inspect` arguments.
type inspectOptions struct {
	Server                 string
	Port                   int
	ListenPort             int
	Token                  string
	TokenFile              string
	DisplayName            string
	Intercept              bool
	InterceptTimeout       float64
	InterceptTimeoutAction string
}

// newInspectCmd wires the `inspect` subcommand (HTTP reverse proxy with traffic
// inspection), mirroring the Python flags/help one-for-one.
func newInspectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "inspect PORT",
		Short:        "HTTP reverse proxy with traffic inspection via tunnel server",
		Long:         "Forward HTTP traffic to a local port through a remote tunnel server with structured inspection.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			port, err := strconv.Atoi(args[0])
			if err != nil {
				return fmt.Errorf("PORT must be an integer: %q", args[0])
			}
			f := cmd.Flags()
			opts := inspectOptions{Port: port}
			opts.Server, _ = f.GetString("server")
			opts.ListenPort, _ = f.GetInt("listen-port")
			opts.Token, _ = f.GetString("token")
			opts.TokenFile, _ = f.GetString("token-file")
			opts.DisplayName, _ = f.GetString("display-name")
			opts.Intercept, _ = f.GetBool("intercept")
			opts.InterceptTimeout, _ = f.GetFloat64("intercept-timeout")
			opts.InterceptTimeoutAction, _ = f.GetString("intercept-timeout-action")
			return runInspect(cmd.Context(), opts, cmd.OutOrStdout(), cmd.ErrOrStderr())
		},
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "CF worker / tunnel server URL")
	f.Int("listen-port", 0, "local proxy listen port (0 = auto-assign, default: 0)")
	f.String("token", "", "bearer token for API auth")
	f.String("token-file", tokenFileDefault(), "path to token file")
	f.String("display-name", "", "override display name (default: http:<port>)")
	f.Bool("intercept", false, "enable HTTP request interception (pause before forwarding)")
	f.Float64("intercept-timeout", 30.0, "seconds to wait for browser action (default: 30)")
	f.String("intercept-timeout-action", "forward", "action on timeout: forward (default) or drop")
	_ = cmd.MarkFlagRequired("server")
	return cmd
}

// runInspect registers an HTTP tunnel, opens the inspection channel, starts the
// action receiver, and serves the reverse proxy until SIGINT/SIGTERM.
func runInspect(ctx context.Context, opts inspectOptions, out, errw io.Writer) error {
	if ctx == nil {
		ctx = context.Background()
	}
	displayName := opts.DisplayName
	if displayName == "" {
		displayName = fmt.Sprintf("http:%d", opts.Port)
	}
	token := readTunnelToken(opts.Token, opts.TokenFile)

	_, _ = fmt.Fprint(out, "Creating tunnel... ")
	info, err := createTunnel(ctx, opts.Server, map[string]any{
		"tunnel_type":  "http",
		"display_name": displayName,
		"local_port":   opts.Port,
	}, token, "uterm-inspect/1.0")
	if err != nil {
		_, _ = fmt.Fprintln(out)
		return err
	}
	if tid := info.resolvedTunnelID(); tid != "" {
		_, _ = fmt.Fprintf(out, "done (%s)\n", tid)
	} else {
		_, _ = fmt.Fprintln(out, "done")
	}
	if info.WSEndpoint == "" {
		return fmt.Errorf("server response missing ws_endpoint")
	}
	wsEndpoint := resolveWSEndpoint(opts.Server, info.WSEndpoint)

	_, _ = fmt.Fprintf(out, "Inspecting HTTP traffic on localhost:%d\n", opts.Port)
	if info.ShareURL != "" {
		_, _ = fmt.Fprintf(out, "  Share: %s\n", info.ShareURL)
	}
	if opts.Intercept {
		_, _ = fmt.Fprintf(out, "  Intercept: ON (timeout: %gs, action: %s)\n",
			opts.InterceptTimeout, opts.InterceptTimeoutAction)
	}
	_, _ = fmt.Fprintln(out, "Press Ctrl+C to stop.")

	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client := tunnelclient.NewClient(wsEndpoint, info.WorkerTok)
	if err := client.Connect(sigCtx); err != nil {
		return fmt.Errorf("cannot connect to tunnel: %w", err)
	}
	defer func() { _ = client.Close() }()
	if err := client.SendRaw(sigCtx, tunnelclient.OpenHTTPFrame(opts.Port)); err != nil {
		return fmt.Errorf("cannot open http channel: %w", err)
	}

	gate := tunnelclient.NewInterceptGate(opts.InterceptTimeout, opts.InterceptTimeoutAction)
	gate.SetEnabled(opts.Intercept)

	sess := &inspectSession{client: client, gate: gate, targetPort: opts.Port, errw: errw}
	sess.broadcastState(sigCtx)

	ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(opts.ListenPort)))
	if err != nil {
		return err
	}
	return sess.serve(sigCtx, ln)
}

// inspectSession holds the mutable state shared between the reverse-proxy
// handler and the ws action receiver for one `uterm inspect` run.
type inspectSession struct {
	client     *tunnelclient.Client
	gate       *tunnelclient.InterceptGate
	targetPort int
	errw       io.Writer

	mu         sync.Mutex
	reqCounter int
}

// nextRID returns the next request id ("r1", "r2", ...), matching inspect.py.
func (s *inspectSession) nextRID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reqCounter++
	return "r" + strconv.Itoa(s.reqCounter)
}

// sendHTTP frames a JSON object on ChannelHTTP (best-effort, like Python's
// suppressed sends).
func (s *inspectSession) sendHTTP(ctx context.Context, obj map[string]any) {
	payload, err := json.Marshal(obj)
	if err != nil {
		return
	}
	_ = s.client.SendData(ctx, payload, tunnelclient.ChannelHTTP)
}

// broadcastState emits the current intercept state on ChannelHTTP.
func (s *inspectSession) broadcastState(ctx context.Context) {
	s.sendHTTP(ctx, map[string]any{
		"type":            "http_intercept_state",
		"enabled":         s.gate.Enabled(),
		"inspect_enabled": s.gate.InspectEnabled(),
		"timeout_s":       s.gate.TimeoutS(),
		"timeout_action":  s.gate.TimeoutAction(),
	})
}

// serve starts the action receiver and runs the reverse-proxy HTTP server on ln
// until ctx is cancelled, then drains gracefully. Exposed for tests binding :0.
func (s *inspectSession) serve(ctx context.Context, ln net.Listener) error {
	recvDone := make(chan struct{})
	go func() {
		defer close(recvDone)
		s.receiveActions(ctx)
	}()

	srv := &http.Server{Handler: http.HandlerFunc(s.handle), ReadHeaderTimeout: 30 * time.Second}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.Serve(ln) }()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}

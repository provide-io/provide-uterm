//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"fmt"
	"io"
	"net"
	"os/signal"
	"syscall"
	"time"

	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/auth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gateway"
)

// listenOptions carries the resolved `uterm listen` arguments.
type listenOptions struct {
	WSURL                     string
	Bind                      string
	TelnetPort                int
	SSHPort                   int
	ServerKey                 string
	ColorMode                 string
	IacNegotiate              bool
	AuthorizedKeys            string
	RequireAuthorizedKeys     bool
	AllowUnauthenticatedSSH   bool
	AllowUnauthenticatedTelne bool
}

// newListenCmd mirrors the Python `listen` subcommand (telnet/SSH listener →
// upstream WS). Flags mirror the Python parser one-for-one.
func newListenCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "listen WS_URL",
		Short:        "telnet/SSH client → remote WS server (start a TCP/SSH listener)",
		Long:         "Accept traditional telnet and/or SSH clients and proxy them to a remote WebSocket terminal server.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			f := cmd.Flags()
			opts := listenOptions{WSURL: args[0]}
			opts.TelnetPort, _ = f.GetInt("port")
			opts.SSHPort, _ = f.GetInt("ssh-port")
			opts.Bind, _ = f.GetString("bind")
			opts.ServerKey, _ = f.GetString("server-key")
			opts.ColorMode, _ = f.GetString("color-mode")
			noIac, _ := f.GetBool("no-iac-negotiate")
			opts.IacNegotiate = !noIac
			opts.AuthorizedKeys, _ = f.GetString("authorized-keys")
			opts.RequireAuthorizedKeys, _ = f.GetBool("require-authorized-keys")
			opts.AllowUnauthenticatedSSH, _ = f.GetBool("allow-unauthenticated-ssh")
			opts.AllowUnauthenticatedTelne, _ = f.GetBool("allow-unauthenticated-telnet")
			return runListen(cmd.Context(), opts, cmd.OutOrStdout())
		},
	}
	f := cmd.Flags()
	f.IntP("port", "p", defaults.GatewayTelnetPort,
		fmt.Sprintf("telnet TCP listen port (0 to disable, default: %d)", defaults.GatewayTelnetPort))
	f.Int("ssh-port", 0, "SSH listen port (0 to disable, default: 0)")
	f.String("bind", defaults.BindAll, fmt.Sprintf("bind address (default: %s)", defaults.BindAll))
	f.String("server-key", "", "SSH host private key file (ephemeral key used if omitted)")
	f.String("color-mode", "passthrough", "ANSI color downgrade mode: passthrough, 256, 16 (default: passthrough)")
	f.Bool("no-iac-negotiate", false, "Disable RFC 1091 TTYPE / RFC 1572 NEW-ENVIRON negotiation on the telnet listener.")
	f.String("authorized-keys", "", "Path to an OpenSSH authorized_keys file used during SSH pubkey auth.")
	f.Bool("require-authorized-keys", false, "Reject SSH connections whose pubkey is not in --authorized-keys. Requires --authorized-keys.")
	f.Bool("allow-unauthenticated-ssh", false, "Explicitly allow an SSH listener without required public-key auth on a non-loopback bind address.")
	f.Bool("allow-unauthenticated-telnet", false, "Explicitly allow the plaintext telnet listener on a non-loopback bind address.")
	return cmd
}

// validateListen enforces the cross-flag rules the Python CLI checks before
// starting any server.
func validateListen(opts listenOptions) (colors.ColorMode, error) {
	if opts.TelnetPort == 0 && opts.SSHPort == 0 {
		return "", fmt.Errorf("at least one of --port or --ssh-port must be non-zero")
	}
	if opts.RequireAuthorizedKeys && opts.AuthorizedKeys == "" {
		return "", fmt.Errorf("--require-authorized-keys requires --authorized-keys to be set")
	}
	switch opts.ColorMode {
	case string(colors.ModePassthrough), string(colors.Mode256), string(colors.Mode16):
		return colors.ColorMode(opts.ColorMode), nil
	default:
		return "", fmt.Errorf("--color-mode must be passthrough, 256, or 16, got %q", opts.ColorMode)
	}
}

// runListen validates flags, binds the requested listeners, and serves them
// until SIGINT/SIGTERM (or the parent context) fires.
func runListen(ctx context.Context, opts listenOptions, out io.Writer) error {
	if ctx == nil {
		ctx = context.Background()
	}
	mode, err := validateListen(opts)
	if err != nil {
		return err
	}
	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	servers, err := buildListenServers(opts, mode)
	if err != nil {
		return err
	}
	if len(servers) == 0 {
		return fmt.Errorf("no servers started")
	}
	return serveListen(sigCtx, servers, out)
}

// listenServer pairs a bound listener with its serve function and banner.
type listenServer struct {
	ln     net.Listener
	serve  func(context.Context, net.Listener) error
	banner string
}

// buildListenServers binds the telnet and/or SSH listeners per the options.
func buildListenServers(opts listenOptions, mode colors.ColorMode) ([]listenServer, error) {
	var servers []listenServer
	if opts.TelnetPort != 0 {
		gw := &gateway.TelnetWsGateway{
			WSURL:                opts.WSURL,
			ColorMode:            mode,
			IacNegotiate:         opts.IacNegotiate,
			IacNegotiateTimeout:  400 * time.Millisecond,
			AllowUnauthenticated: opts.AllowUnauthenticatedTelne,
			ReconnectDelay:       3 * time.Second,
			MaxReconnects:        12,
		}
		ln, err := gw.Start(opts.Bind, opts.TelnetPort)
		if err != nil {
			closeAll(servers)
			return nil, err
		}
		servers = append(servers, listenServer{
			ln:     ln,
			serve:  gw.Serve,
			banner: fmt.Sprintf("uterm listen  telnet://%s:%d  →  %s", opts.Bind, opts.TelnetPort, opts.WSURL),
		})
	}
	if opts.SSHPort != 0 {
		srv, err := buildSSHServer(opts, mode)
		if err != nil {
			closeAll(servers)
			return nil, err
		}
		servers = append(servers, srv)
	}
	return servers, nil
}

// buildSSHServer binds the SSH gateway listener with its optional resolver.
func buildSSHServer(opts listenOptions, mode colors.ColorMode) (listenServer, error) {
	var resolver auth.SSHKeyResolver
	if opts.AuthorizedKeys != "" {
		resolver = auth.NewAuthorizedKeysFileResolver(opts.AuthorizedKeys)
	}
	gw := &gateway.SshWsGateway{
		WSURL:                opts.WSURL,
		ColorMode:            mode,
		ServerKey:            opts.ServerKey,
		KeyResolver:          resolver,
		RequireResolver:      opts.RequireAuthorizedKeys,
		AllowUnauthenticated: opts.AllowUnauthenticatedSSH,
		ReconnectDelay:       3 * time.Second,
		MaxReconnects:        12,
	}
	ln, err := gw.Start(opts.Bind, opts.SSHPort)
	if err != nil {
		return listenServer{}, err
	}
	suffix := ""
	if opts.AuthorizedKeys != "" {
		m := "optional"
		if opts.RequireAuthorizedKeys {
			m = "required"
		}
		suffix = fmt.Sprintf("   [pubkey: %s (%s)]", opts.AuthorizedKeys, m)
	}
	return listenServer{
		ln:     ln,
		serve:  gw.Serve,
		banner: fmt.Sprintf("uterm listen  ssh://%s:%d     →  %s%s", opts.Bind, opts.SSHPort, opts.WSURL, suffix),
	}, nil
}

func closeAll(servers []listenServer) {
	for _, s := range servers {
		_ = s.ln.Close()
	}
}

// serveListen serves every bound listener until ctx is cancelled.
func serveListen(ctx context.Context, servers []listenServer, out io.Writer) error {
	errCh := make(chan error, len(servers))
	for _, s := range servers {
		_, _ = fmt.Fprintln(out, s.banner)
		go func(s listenServer) { errCh <- s.serve(ctx, s.ln) }(s)
	}
	select {
	case <-ctx.Done():
		closeAll(servers)
		return nil
	case err := <-errCh:
		closeAll(servers)
		return err
	}
}

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
	"strconv"
	"sync"
	"syscall"

	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// tunnelOptions carries the resolved `uterm tunnel` arguments.
type tunnelOptions struct {
	Server      string
	Port        int
	Token       string
	TokenFile   string
	DisplayName string
}

// newTunnelCmd wires the `tunnel` subcommand (forward a local TCP port through a
// tunnel server), mirroring the Python flags/help one-for-one.
func newTunnelCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tunnel PORT",
		Short:        "forward a local TCP port via tunnel server",
		Long:         "Forward a local TCP port through a remote tunnel server.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			port, err := strconv.Atoi(args[0])
			if err != nil {
				return fmt.Errorf("PORT must be an integer: %q", args[0])
			}
			f := cmd.Flags()
			opts := tunnelOptions{Port: port}
			opts.Server, _ = f.GetString("server")
			opts.Token, _ = f.GetString("token")
			opts.TokenFile, _ = f.GetString("token-file")
			opts.DisplayName, _ = f.GetString("display-name")
			return runTunnel(cmd.Context(), opts, cmd.OutOrStdout())
		},
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "CF worker / tunnel server URL")
	f.String("token", "", "bearer token for API auth")
	f.String("token-file", tokenFileDefault(), "path to token file")
	f.String("display-name", "", "override display name (default: tcp:<port>)")
	_ = cmd.MarkFlagRequired("server")
	return cmd
}

// runTunnel registers a TCP tunnel, opens the tunnel channel, binds the local
// listener, and relays connections through the tunnel until SIGINT/SIGTERM.
func runTunnel(ctx context.Context, opts tunnelOptions, out io.Writer) error {
	if ctx == nil {
		ctx = context.Background()
	}
	displayName := opts.DisplayName
	if displayName == "" {
		displayName = fmt.Sprintf("tcp:%d", opts.Port)
	}
	token := readTunnelToken(opts.Token, opts.TokenFile)
	info, err := createTunnel(ctx, opts.Server, map[string]any{
		"tunnel_type":  "tcp",
		"display_name": displayName,
		"local_port":   opts.Port,
	}, token, "uterm-tunnel/1.0")
	if err != nil {
		return err
	}
	if info.WSEndpoint == "" {
		return fmt.Errorf("server response missing ws_endpoint")
	}
	wsEndpoint := resolveWSEndpoint(opts.Server, info.WSEndpoint)

	_, _ = fmt.Fprintf(out, "Tunneling localhost:%d...\n", opts.Port)
	if info.ShareURL != "" {
		_, _ = fmt.Fprintf(out, "  Share: %s\n", info.ShareURL)
	}
	_, _ = fmt.Fprintln(out, "\nConnected. Press Ctrl+C to stop.")

	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client := tunnelclient.NewClient(wsEndpoint, info.WorkerTok)
	if err := client.Connect(sigCtx); err != nil {
		return fmt.Errorf("cannot connect to tunnel: %w", err)
	}
	defer func() { _ = client.Close() }()
	if err := client.SendRaw(sigCtx, tunnelclient.OpenTCPFrame(opts.Port)); err != nil {
		return fmt.Errorf("cannot open tcp channel: %w", err)
	}

	ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(opts.Port)))
	if err != nil {
		return err
	}
	return serveTunnel(sigCtx, ln, client)
}

// serveTunnel accepts local TCP connections and relays each through the tunnel
// until ctx is cancelled. Like the Python CLI, a single CHANNEL_TCP stream is
// multiplexed, so it is designed for one active connection at a time. Exposed
// for tests that bind :0.
func serveTunnel(ctx context.Context, ln net.Listener, client *tunnelclient.Client) error {
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		handleTunnelConn(ctx, conn, client)
	}
}

// handleTunnelConn bridges one local TCP connection through the tunnel: local
// bytes are framed on ChannelTCP, and inbound ChannelTCP frames are written back
// to the connection until either side ends.
func handleTunnelConn(ctx context.Context, conn net.Conn, client *tunnelclient.Client) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer func() { _ = conn.Close() }()

	var wg sync.WaitGroup
	wg.Add(2)

	// local TCP → WS (ChannelTCP frames, EOF on close)
	go func() {
		defer wg.Done()
		defer cancel()
		buf := make([]byte, 4096)
		for {
			n, rerr := conn.Read(buf)
			if n > 0 {
				if err := client.SendData(ctx, buf[:n], tunnelclient.ChannelTCP); err != nil {
					return
				}
			}
			if rerr != nil {
				_ = client.SendEOF(ctx, tunnelclient.ChannelTCP)
				return
			}
		}
	}()

	// WS (ChannelTCP frames) → local TCP
	go func() {
		defer wg.Done()
		defer cancel()
		for {
			frame, rerr := client.Recv(ctx)
			if rerr != nil {
				return
			}
			if frame.Channel != tunnelclient.ChannelTCP {
				continue
			}
			if frame.IsEOF() {
				return
			}
			if _, werr := conn.Write(frame.Payload); werr != nil {
				return
			}
		}
	}()

	wg.Wait()
}

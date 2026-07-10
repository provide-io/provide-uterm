//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"os/user"
	"sync"
	"syscall"

	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnelclient"
)

// shareOptions carries the resolved `uterm share` arguments.
type shareOptions struct {
	Server      string
	Cmd         []string
	Token       string
	TokenFile   string
	Attach      bool
	DisplayName string
}

// newShareCmd wires the `share` subcommand (PTY → tunnel WS → shareable URL),
// mirroring the Python flags/help one-for-one.
func newShareCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "share [CMD ...]",
		Short:        "share a terminal session via tunnel server",
		Long:         "Spawn a PTY (or attach to current TTY) and share via a remote tunnel.",
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			f := cmd.Flags()
			opts := shareOptions{Cmd: args}
			opts.Server, _ = f.GetString("server")
			opts.Token, _ = f.GetString("token")
			opts.TokenFile, _ = f.GetString("token-file")
			opts.Attach, _ = f.GetBool("attach")
			opts.DisplayName, _ = f.GetString("display-name")
			return runShare(cmd.Context(), opts, cmd.OutOrStdout())
		},
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "CF worker / tunnel server URL")
	f.String("token", "", "bearer token for API auth")
	f.String("token-file", tokenFileDefault(), fmt.Sprintf("path to token file (default: %s)", tokenFileDefault()))
	f.Bool("attach", false, "attach to current TTY instead of spawning a new PTY")
	f.String("display-name", "", "override display name (default: user@hostname)")
	_ = cmd.MarkFlagRequired("server")
	return cmd
}

// shareDisplayName builds the display name from --display-name or user@hostname.
func shareDisplayName(override string) string {
	if override != "" {
		return override
	}
	name := "unknown"
	if u, err := user.Current(); err == nil && u.Username != "" {
		name = u.Username
	}
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "localhost"
	}
	return name + "@" + host
}

// runShare registers a terminal tunnel, prints the shareable URLs, spawns a PTY
// (or attaches to the current TTY), and bridges it to the tunnel until the child
// exits or SIGINT/SIGTERM fires.
func runShare(ctx context.Context, opts shareOptions, out io.Writer) error {
	if ctx == nil {
		ctx = context.Background()
	}
	token := readTunnelToken(opts.Token, opts.TokenFile)
	info, err := createTunnel(ctx, opts.Server, map[string]any{
		"tunnel_type":  "terminal",
		"display_name": shareDisplayName(opts.DisplayName),
	}, token, "uterm-share/1.0")
	if err != nil {
		return err
	}
	if info.WSEndpoint == "" {
		return fmt.Errorf("server response missing ws_endpoint")
	}
	wsEndpoint := resolveWSEndpoint(opts.Server, info.WSEndpoint)

	_, _ = fmt.Fprintln(out, "Sharing terminal session...")
	_, _ = fmt.Fprintf(out, "  View:    %s\n", info.ShareURL)
	_, _ = fmt.Fprintf(out, "  Control: %s\n", info.ControlURL)
	_, _ = fmt.Fprintln(out)
	_, _ = fmt.Fprintln(out, "Connected. Press Ctrl+C to stop sharing.")

	src, err := openShareSource(opts)
	if err != nil {
		return err
	}
	defer func() { _ = src.Close() }()

	sigCtx, stop := signal.NotifyContext(ctx, syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client := tunnelclient.NewClient(wsEndpoint, info.WorkerTok)
	if err := client.Connect(sigCtx); err != nil {
		return fmt.Errorf("cannot connect to tunnel: %w", err)
	}
	defer func() { _ = client.Close() }()

	bridgeShare(sigCtx, client, src)
	return nil
}

// openShareSource returns a TtyProxy in --attach mode, else a freshly spawned
// PTY child running the requested command (or $SHELL).
func openShareSource(opts shareOptions) (tunnelclient.PTYSource, error) {
	if opts.Attach {
		p := tunnelclient.NewTtyProxy()
		if _, _, err := p.Start(); err != nil {
			return nil, fmt.Errorf("cannot enter raw mode: %w", err)
		}
		return p, nil
	}
	return tunnelclient.SpawnPTY(opts.Cmd)
}

// bridgeShare pumps bytes both ways between the PTY source and the tunnel until
// either side ends. PTY output is framed on ChannelData; inbound WS bytes are
// written raw to the source (matching share.py's inbound path). When either
// goroutine ends it cancels the shared context so the other unblocks — a cleaner
// shutdown than Python's gather, which relies on the server closing the socket.
func bridgeShare(ctx context.Context, client *tunnelclient.Client, src tunnelclient.PTYSource) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	var wg sync.WaitGroup
	wg.Add(2)

	// PTY → WS
	go func() {
		defer wg.Done()
		defer cancel()
		buf := make([]byte, 4096)
		for {
			n, rerr := src.Read(buf)
			if n > 0 {
				if err := client.SendData(ctx, buf[:n], tunnelclient.ChannelData); err != nil {
					return
				}
			}
			if rerr != nil {
				_ = client.SendEOF(ctx, tunnelclient.ChannelData)
				return
			}
		}
	}()

	// WS → PTY
	go func() {
		defer wg.Done()
		defer cancel()
		for {
			data, rerr := client.RecvRaw(ctx)
			if rerr != nil {
				return
			}
			if len(data) == 0 {
				continue
			}
			if _, werr := src.Write(data); werr != nil {
				return
			}
		}
	}()

	wg.Wait()
}

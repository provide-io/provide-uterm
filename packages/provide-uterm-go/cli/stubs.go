//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// stubErr is the error every not-yet-ported subcommand returns. It carries the
// subcommand name so the message names the exact command.
type stubErr struct{ cmd string }

func (e stubErr) Error() string {
	return "uterm " + e.cmd + ": not yet available in the Go build"
}

// stubRunE returns a RunE that reports the command is unavailable. The Go build
// only wires the subcommands whose dependencies have been ported (server,
// proxy); the rest exist to mirror the Python help surface exactly.
func stubRunE(name string) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, _ []string) error { return stubErr{cmd: name} }
}

// tokenFileDefault returns the default resume-token file path used in help text
// (mirrors TerminalDefaults.token_file()). It degrades to the bare relative
// hint when the home directory cannot be resolved.
func tokenFileDefault() string {
	if p, err := defaults.TokenFile(); err == nil {
		return p
	}
	return ".uterm/session_token"
}

// newShareCmd mirrors the Python `share` subcommand (PTY → tunnel WS →
// shareable URL). The tunnel client is not yet ported, so it is a stub.
func newShareCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "share [CMD ...]",
		Short:        "share a terminal session via tunnel server",
		Long:         "Spawn a PTY (or attach to current TTY) and share via a remote tunnel.",
		SilenceUsage: true,
		RunE:         stubRunE("share"),
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "CF worker / tunnel server URL")
	f.String("token", "", "bearer token for API auth")
	f.String("token-file", tokenFileDefault(), fmt.Sprintf("path to token file (default: %s)", tokenFileDefault()))
	f.Bool("attach", false, "attach to current TTY instead of spawning a new PTY")
	f.String("display-name", "", "override display name (default: user@hostname)")
	return cmd
}

// newTunnelCmd mirrors the Python `tunnel` subcommand (forward a local TCP port
// through a tunnel server). The tunnel client is not yet ported, so it is a stub.
func newTunnelCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tunnel PORT",
		Short:        "forward a local TCP port via tunnel server",
		Long:         "Forward a local TCP port through a remote tunnel server.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE:         stubRunE("tunnel"),
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "CF worker / tunnel server URL")
	f.String("token", "", "bearer token for API auth")
	f.String("token-file", tokenFileDefault(), "path to token file")
	f.String("display-name", "", "override display name (default: tcp:<port>)")
	return cmd
}

// newInspectCmd mirrors the Python `inspect` subcommand (HTTP reverse proxy with
// traffic inspection). The tunnel client is not yet ported, so it is a stub.
func newInspectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "inspect PORT",
		Short:        "HTTP reverse proxy with traffic inspection via tunnel server",
		Long:         "Forward HTTP traffic to a local port through a remote tunnel server with structured inspection.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE:         stubRunE("inspect"),
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
	return cmd
}

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

// newListenCmd mirrors the Python `listen` subcommand (telnet/SSH listener →
// upstream WS). The gateway package is not yet ported, so it is a stub.
func newListenCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "listen WS_URL",
		Short:        "telnet/SSH client → remote WS server (start a TCP/SSH listener)",
		Long:         "Accept traditional telnet and/or SSH clients and proxy them to a remote WebSocket terminal server.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE:         stubRunE("listen"),
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

// newWatchCmd mirrors the Python `watch` subcommand (TUI HTTP traffic viewer).
// The TUI is not yet ported, so it is a stub.
func newWatchCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "watch TUNNEL",
		Short:        "TUI HTTP traffic viewer for tunnel sessions",
		Long:         "Connect to an existing tunnel and watch HTTP traffic in a terminal UI.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE:         stubRunE("watch"),
	}
	f := cmd.Flags()
	f.StringP("server", "s", "", "tunnel server URL")
	f.String("layout", "horizontal", "initial layout mode: horizontal, vertical, modal")
	f.String("token", "", "bearer token for auth")
	f.String("token-file", tokenFileDefault(), "path to token file")
	return cmd
}

// newAuditCmd mirrors the Python `audit` subcommand and its nested `verify`
// action. The hash-chain verifier requires byte-exact parity with Python's
// float JSON canonicalization (json.dumps default=str), which is not safely
// reproducible in Go without the same repr — so verification is a stub to
// avoid falsely reporting valid logs as tampered.
func newAuditCmd() *cobra.Command {
	audit := &cobra.Command{
		Use:   "audit",
		Short: "verify a tamper-evident WORM audit log",
		Long:  "Verify the integrity of a hash-chained append-only audit log.",
	}
	verify := &cobra.Command{
		Use:          "verify PATH",
		Short:        "verify the hash chain of an audit log file",
		Long:         "Walk the audit log and confirm no record was inserted, deleted, reordered, or altered.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE:         stubRunE("audit verify"),
	}
	vf := verify.Flags()
	vf.Int("expected-seq", 0, "expected head sequence number (requires --expected-hash)")
	vf.String("expected-hash", "", "expected head record hash (requires --expected-seq)")
	audit.AddCommand(verify)
	return audit
}

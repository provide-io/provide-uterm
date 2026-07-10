//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package cli implements the `uterm` command tree. The binary in
// cmd/uterm is a thin wrapper around [Execute]; every subcommand's logic
// lives here so it stays unit-testable.
//
// The subcommand surface mirrors the Python `uterm` CLI (provide.uterm.cli)
// one-for-one: proxy, listen, share, tunnel, inspect, watch, audit, server.
// Only the subcommands whose Go dependencies have been ported are wired for
// real (server, proxy); the rest render identical help but report that they
// are not yet available in the Go build and exit non-zero.
package cli

import (
	"errors"
	"io"

	"github.com/spf13/cobra"
)

// Version is the reported binary version. Override at build time with
// -ldflags "-X .../cli.Version=x.y.z". It mirrors the server.New default.
var Version = "0.0.0-dev"

// NewRootCmd assembles the full `uterm` command tree. Commands are added in
// the same order the Python argparse parser registers them, and command
// sorting is disabled so `uterm --help` lists them in that order.
func NewRootCmd() *cobra.Command {
	cobra.EnableCommandSorting = false

	root := &cobra.Command{
		Use:           "uterm",
		Short:         "Bidirectional WebSocket terminal proxy for BBS/telnet servers.",
		Long:          "Bidirectional WebSocket terminal proxy for BBS/telnet servers.",
		Version:       Version,
		SilenceErrors: true,
		SilenceUsage:  true,
	}
	// No default `completion` command — the Python CLI has no equivalent.
	root.CompletionOptions.DisableDefaultCmd = true

	root.AddCommand(newProxyCmd())
	root.AddCommand(newListenCmd())
	root.AddCommand(newShareCmd())
	root.AddCommand(newTunnelCmd())
	root.AddCommand(newInspectCmd())
	root.AddCommand(newWatchCmd())
	root.AddCommand(newAuditCmd())
	root.AddCommand(newServerCmd())
	return root
}

// Execute runs the root command against args, writing to out/errw, and returns
// a process exit code (0 = success, non-zero = failure). main passes os.Args
// (minus argv0), os.Stdout and os.Stderr.
func Execute(args []string, out, errw io.Writer) int {
	root := NewRootCmd()
	root.SetArgs(args)
	root.SetOut(out)
	root.SetErr(errw)
	if err := root.Execute(); err != nil {
		// A silent error (e.g. audit TAMPERED) already wrote its own report to
		// stdout and only needs the non-zero exit code.
		var se *silentError
		if errors.As(err, &se) {
			return 1
		}
		// cobra silences its own printing (SilenceErrors); surface the error
		// message ourselves so both usage errors and command failures report.
		if _, werr := io.WriteString(errw, "error: "+err.Error()+"\n"); werr != nil {
			return 1
		}
		return 1
	}
	return 0
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package livedriver

import (
	"context"
	"errors"
	"io"

	"github.com/spf13/cobra"
)

// NewRootCmd assembles the driver's two-subcommand tree. stdin and stdout are
// the protocol channels: stdout carries exactly one line of JSON per run, so
// nothing else may be written to it.
func NewRootCmd(stdin io.Reader, stdout io.Writer) *cobra.Command {
	cobra.EnableCommandSorting = false
	root := &cobra.Command{
		Use:   "uterm-live-driver",
		Short: "Go driver for the cross-language live conformance harness",
		Long: "Go driver for the cross-language live conformance harness " +
			"(conformance/live/PROTOCOL.md). It observes; the harness judges.",
		SilenceErrors: true,
		SilenceUsage:  true,
		// Without a role there is nothing to do, and a driver that exited 0
		// having done nothing would read to the harness as a successful run.
		RunE: func(*cobra.Command, []string) error {
			return errors.New("a role is required: serve or client")
		},
	}
	root.CompletionOptions.DisableDefaultCmd = true
	root.AddCommand(newServeCmd(stdin, stdout))
	root.AddCommand(newClientCmd(stdout))
	return root
}

// newServeCmd registers `serve`.
func newServeCmd(stdin io.Reader, stdout io.Writer) *cobra.Command {
	cmd := &cobra.Command{
		Use:          "serve",
		Short:        "run the Go uterm server on an ephemeral port",
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			auth, _ := cmd.Flags().GetString("auth")
			config, _ := cmd.Flags().GetString("config")
			return RunServe(cmd.Context(), ServeOptions{AuthMode: auth, ConfigPath: config}, stdin, stdout)
		},
	}
	f := cmd.Flags()
	f.String("auth", DefaultAuthMode, "auth mode to start the server in")
	f.String("config", "", "optional TOML server config; empty uses the defaults")
	// Accepted because the protocol allows `serve [--scenario FILE]`, and
	// ignored because the server role performs no steps.
	f.String("scenario", "", "scenario file (accepted for protocol compatibility; unused)")
	return cmd
}

// newClientCmd registers `client`.
func newClientCmd(stdout io.Writer) *cobra.Command {
	cmd := &cobra.Command{
		Use:          "client",
		Short:        "run a scenario against a running server driver",
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			baseURL, _ := cmd.Flags().GetString("base-url")
			token, _ := cmd.Flags().GetString("token")
			scenario, _ := cmd.Flags().GetString("scenario")
			return RunClient(cmd.Context(), ClientOptions{
				BaseURL:      baseURL,
				Token:        token,
				ScenarioPath: scenario,
			}, stdout)
		},
	}
	f := cmd.Flags()
	f.String("base-url", "", "origin reported by the server driver")
	f.String("token", "", "bearer token reported by the server driver")
	f.String("scenario", "", "scenario file to run")
	must(cmd.MarkFlagRequired("base-url"))
	must(cmd.MarkFlagRequired("scenario"))
	return cmd
}

// must panics on an error that only a programming mistake can produce (marking
// a flag that was just registered as required).
func must(err error) {
	if err != nil {
		panic(err)
	}
}

// Execute runs the driver and returns a process exit code. Usage, help and
// errors all go to stderr: stdout belongs to the protocol.
//
// The exit code says whether a report was produced, not whether the scenario
// passed — a result whose status is "error" is still a report, and is still
// exit 0, because the harness reads the verdict from the JSON.
func Execute(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	root := NewRootCmd(stdin, stdout)
	root.SetArgs(args)
	root.SetOut(stderr)
	root.SetErr(stderr)
	if err := root.ExecuteContext(ctx); err != nil {
		_, _ = io.WriteString(stderr, "error: "+err.Error()+"\n")
		return 1
	}
	return 0
}

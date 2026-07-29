//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Command uterm-live-driver is the Go driver for the cross-language live
// conformance harness (conformance/live/PROTOCOL.md). It is a thin wrapper
// around the livedriver package, which owns both roles so they stay
// unit-testable.
//
//	uterm-live-driver serve  [--auth MODE] [--config FILE]
//	uterm-live-driver client --base-url URL --token TOKEN --scenario FILE
package main

import (
	"context"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/livedriver"
)

// devTokenPathEnv redirects where the dev IdP writes its last-issued token.
const devTokenPathEnv = "UTERM_DEV_TOKEN_PATH"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cleanup, err := isolateDevToken()
	if err != nil {
		_, _ = os.Stderr.WriteString("error: " + err.Error() + "\n")
		os.Exit(1)
	}
	defer cleanup()

	os.Exit(livedriver.Execute(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// isolateDevToken points the dev IdP's token file at a throwaway directory, so
// a harness run never overwrites the developer's own ~/.uterm dev token. An
// explicit setting from the environment is left alone.
func isolateDevToken() (func(), error) {
	if os.Getenv(devTokenPathEnv) != "" {
		return func() {}, nil
	}
	dir, err := os.MkdirTemp("", "uterm-live-driver-")
	if err != nil {
		return nil, err
	}
	if err := os.Setenv(devTokenPathEnv, filepath.Join(dir, "dev_token")); err != nil {
		_ = os.RemoveAll(dir)
		return nil, err
	}
	return func() {
		_ = os.Unsetenv(devTokenPathEnv)
		_ = os.RemoveAll(dir)
	}, nil
}

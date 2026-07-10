//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Command uterm is the terminal session platform CLI. It is a thin wrapper
// around the cli package, which owns the full cobra command tree so the
// command logic stays unit-testable.
package main

import (
	"os"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/cli"
)

func main() {
	os.Exit(cli.Execute(os.Args[1:], os.Stdout, os.Stderr))
}

// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package uterm holds the release version for the Go port.
//
// It exists at the module root for one reason: go:embed can only reach files in
// or below the embedding package's directory, and the VERSION file has to live
// at packages/provide-uterm-go/VERSION so the release consistency check finds
// it. That check walks packages/*/VERSION, and this port had no such file, so
// nothing compared it to anything -- cli.Version and the server default sat at
// "0.0.0-dev" and the MCP server at "0.1.0" while the Python packages went to
// 0.5.4. Reading the file is what keeps them from drifting again.
package uterm

import (
	_ "embed"
	"strings"
)

//go:embed VERSION
var versionFile string

// Version is the release version of this port, read from VERSION at build time.
var Version = strings.TrimSpace(versionFile)

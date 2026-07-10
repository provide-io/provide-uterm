//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// tokenFileDefault returns the default resume-token file path used in help text
// (mirrors TerminalDefaults.token_file()). It degrades to the bare relative
// hint when the home directory cannot be resolved. Shared by the share, tunnel,
// inspect and watch subcommands.
func tokenFileDefault() string {
	if p, err := defaults.TokenFile(); err == nil {
		return p
	}
	return ".uterm/session_token"
}

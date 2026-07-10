//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"fmt"
	"strings"
)

// ValidateSocketPath rejects Unix socket paths that aren't absolute or contain
// null bytes. Port of socket_utils.validate_socket_path.
func ValidateSocketPath(path string) error {
	if strings.ContainsRune(path, '\x00') {
		return fmt.Errorf("socket path contains null byte")
	}
	if !strings.HasPrefix(path, "/") {
		return fmt.Errorf("socket path must be an absolute path")
	}
	return nil
}

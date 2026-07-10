//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"fmt"
	"regexp"
	"strings"
)

// Validation bounds — port of _validate.py module constants.
const (
	maxPathLen     = 4096
	maxUsernameLen = 255
	maxServiceLen  = 255
	maxEnvKeys     = 1000
	maxEnvValueLen = 65536
)

// usernameRE matches POSIX portable filename characters for usernames.
var usernameRE = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// serviceRE matches PAM service names: letters, digits, hyphens, underscores.
var serviceRE = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// ValidateCommand validates a command path for use with execve. Port of
// _validate.validate_command.
func ValidateCommand(command string) error {
	if command == "" {
		return fmt.Errorf("command must not be empty")
	}
	if strings.ContainsRune(command, '\x00') {
		return fmt.Errorf("command contains null byte")
	}
	if len(command) > maxPathLen {
		return fmt.Errorf("command path too long (max %d chars)", maxPathLen)
	}
	if !strings.HasPrefix(command, "/") {
		return fmt.Errorf(
			"command must be an absolute path (got %q); relative paths and shell lookups are not allowed",
			command,
		)
	}
	return nil
}

// ValidateUsername validates an OS username. Port of _validate.validate_username.
func ValidateUsername(username string) error {
	if username == "" {
		return fmt.Errorf("username must not be empty")
	}
	if strings.ContainsRune(username, '\x00') {
		return fmt.Errorf("username contains null byte")
	}
	if len(username) > maxUsernameLen {
		return fmt.Errorf("username too long (max %d chars)", maxUsernameLen)
	}
	if !usernameRE.MatchString(username) {
		return fmt.Errorf(
			"username %q contains invalid character; only A-Z, a-z, 0-9, '.', '_', '-' are allowed",
			username,
		)
	}
	return nil
}

// ValidateServiceName validates a PAM service name. Port of
// _validate.validate_service_name.
func ValidateServiceName(service string) error {
	if service == "" {
		return fmt.Errorf("PAM service name must not be empty")
	}
	if strings.ContainsRune(service, '\x00') {
		return fmt.Errorf("PAM service name contains null byte")
	}
	if len(service) > maxServiceLen {
		return fmt.Errorf("PAM service name too long (max %d chars)", maxServiceLen)
	}
	if !serviceRE.MatchString(service) {
		return fmt.Errorf(
			"PAM service name %q contains invalid character; only A-Z, a-z, 0-9, '_', '-' are allowed",
			service,
		)
	}
	return nil
}

// ValidateEnv validates an environment map for use with execve. Port of
// _validate.validate_env.
func ValidateEnv(env map[string]string) error {
	if len(env) > maxEnvKeys {
		return fmt.Errorf("env dict has too many keys (max %d)", maxEnvKeys)
	}
	for key, value := range env {
		if strings.ContainsRune(key, '=') {
			return fmt.Errorf("invalid key %q: env keys must not contain '='", key)
		}
		if strings.ContainsRune(key, '\x00') {
			return fmt.Errorf("env key %q contains null byte", key)
		}
		if strings.ContainsRune(value, '\x00') {
			return fmt.Errorf("env value for %q contains null byte", key)
		}
		if len(value) > maxEnvValueLen {
			return fmt.Errorf("env value for %q too long (max %d chars)", key, maxEnvValueLen)
		}
	}
	return nil
}

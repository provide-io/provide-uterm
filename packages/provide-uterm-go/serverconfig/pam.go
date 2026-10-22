//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// OwnerOnlyCaptureSocketMode and OwnerAndGroupCaptureSocketMode are the modes
// pam.capture_socket_mode may name: owner-only (the default), or owner and
// group. The pty package's CheckSocketMode accepts the same two; it
// is not imported here, since a config package has no business pulling in the
// one that binds sockets.
const (
	OwnerOnlyCaptureSocketMode     = 0o600
	OwnerAndGroupCaptureSocketMode = 0o660
)

// errCaptureSocketModeNotAnInteger is the reference's refusal of a
// pam.capture_socket_mode that is not an integer.
var errCaptureSocketModeNotAnInteger = errors.New("pam.capture_socket_mode must be an integer: 0o600 or 0o660")

// refuseCaptureSocketModeType refuses a pam.capture_socket_mode that is not a
// TOML integer, as the reference's before-validator does ahead of Pydantic's
// own int parsing. Checked before the sections are decoded, because
// decodeSection's JSON round trip turns 432.0 into 432 and would read it as a
// mode, where the reference refuses a float.
func refuseCaptureSocketModeType(data map[string]any) error {
	pam, _ := data["pam"].(map[string]any)
	value, present := pam["capture_socket_mode"]
	if !present {
		return nil
	}
	switch value.(type) {
	case int, int64:
		return nil
	default:
		return errCaptureSocketModeNotAnInteger
	}
}

// validateCaptureSocketMode refuses any mode but the two a capture socket may
// be bound with, quoted as Python's oct() writes it.
func validateCaptureSocketMode(mode int) error {
	if mode == OwnerOnlyCaptureSocketMode || mode == OwnerAndGroupCaptureSocketMode {
		return nil
	}
	digits := strconv.FormatInt(int64(mode), 8)
	shown := "0o" + digits
	if rest, negative := strings.CutPrefix(digits, "-"); negative {
		shown = "-0o" + rest
	}
	return fmt.Errorf("pam.capture_socket_mode must be 0o600 or 0o660, got: %s", shown)
}

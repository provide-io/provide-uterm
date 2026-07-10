//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

//go:build !unix

package manager

import (
	"errors"
	"syscall"
)

// newSysProcAttr is a no-op on non-unix platforms. The manager targets unix;
// the Windows taskkill /T /F path from the Python port is not implemented here.
func newSysProcAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{} }

// signalGroupByPID is unsupported on non-unix platforms.
func signalGroupByPID(_ int, _ syscall.Signal) error {
	return errors.New("process-group signalling is only supported on unix")
}

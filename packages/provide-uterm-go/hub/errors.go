//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "fmt"

// InvalidInputModeError is returned by [TermHub.SetWorkerHelloMode] for a mode
// string outside {"hijack","open"}. Port of the ValueError raised by
// core_orchestration.set_worker_hello_mode.
type InvalidInputModeError struct {
	Mode string
}

func (e *InvalidInputModeError) Error() string {
	return fmt.Sprintf("invalid input mode: %q", e.Mode)
}

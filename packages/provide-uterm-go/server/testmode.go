// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package server

// UTERM_TEST_MODE mints an admin principal for browser websockets, skipping
// authentication and authorization for any worker id. It exists for the
// multi-backend Playwright suite, and the only thing standing between it and a
// production server was a comment saying not to set it — none of the three
// ports announced that the gate was open, so a server accidentally started with
// it looked exactly like one that was not.
//
// The wording is identical in the C# (Server/TestModeBanner.cs) and Python
// (server/cli.py) ports so the string greps across logs from any backend.
const (
	testModeEnvVar  = "UTERM_TEST_MODE"
	testModeWarning = "WARNING: UTERM_TEST_MODE=1 — websocket authentication is DISABLED and an admin " +
		"principal is minted for any session. For tests only; never run a production server this way."
)

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Server;

/// <summary>
/// The line a server prints when <c>UTERM_TEST_MODE=1</c> is in its environment.
/// </summary>
/// <remarks>
/// That variable mints an admin principal for browser websockets, skipping
/// authentication and authorization for any worker id. It exists for the
/// multi-backend Playwright suite, and the only thing standing between it and a
/// production server was a comment saying not to set it — nothing in any of the
/// three ports announced that the gate was open, so a server accidentally
/// started with it looked exactly like one that was not.
///
/// Kept as a shared constant so the wording is identical in Go and Python and
/// the string is greppable across logs from any backend.
/// </remarks>
public static class TestModeBanner
{
    /// <summary>Environment variable that disables websocket authentication.</summary>
    public const string EnvVar = "UTERM_TEST_MODE";

    /// <summary>Warning printed at startup while <see cref="EnvVar"/> is set to 1.</summary>
    public const string Warning =
        "WARNING: UTERM_TEST_MODE=1 — websocket authentication is DISABLED and an admin "
        + "principal is minted for any session. For tests only; never run a production server this way.";
}

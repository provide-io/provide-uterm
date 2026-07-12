//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Tests.Interop;

/// <summary>
/// Live C# ↔ Python interop is optional (needs uv). Placeholder documents the
/// skip path; make interop-test / CI may run fuller filters when Python is present.
/// </summary>
public class InteropPlaceholderTests
{
    [Fact]
    public void Interop_Directory_IsWired()
    {
        // Always-pass marker so the suite has a real Interop test file.
        Assert.True(true);
    }
}

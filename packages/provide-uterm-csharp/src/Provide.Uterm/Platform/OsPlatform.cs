//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Runtime.Versioning;

namespace Provide.Uterm.Platform;

/// <summary>
/// The one place the library asks which OS it runs on, for the decisions whose
/// two arms are otherwise each reachable on only one platform.
///
/// <see cref="IsWindowsProbe"/> is a test seam defaulting to the real check, so
/// both arms of every such decision run on every CI OS. Tests that replace it
/// restore it in a <c>finally</c> (the test host never runs tests in parallel).
/// </summary>
internal static class OsPlatform
{
    internal static Func<bool> IsWindowsProbe { get; set; } = OperatingSystem.IsWindows;

    /// <summary>True on Windows (or when a test says so).</summary>
    [SupportedOSPlatformGuard("windows")]
    internal static bool IsWindows => IsWindowsProbe();

    /// <summary>True where POSIX file modes exist: everywhere but Windows.</summary>
    [UnsupportedOSPlatformGuard("windows")]
    internal static bool HasUnixFileModes => !IsWindowsProbe();
}

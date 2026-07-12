//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Shell;

/// <summary>Animated render/cast output — caller handles frame timing. Port of Go shell.AnimatedResult.</summary>
public sealed class AnimatedResult
{
    public IReadOnlyList<string> Frames { get; init; } = Array.Empty<string>();
    public double Fps { get; init; }
    public bool Loop { get; init; }
}

/// <summary>Return value of a dispatched command. Port of Go shell.Result.</summary>
public sealed class ShellResult
{
    public IReadOnlyList<string> Text { get; init; } = Array.Empty<string>();
    public AnimatedResult? Animated { get; init; }

    public static ShellResult OfText(params string[] frames) => new() { Text = frames };

    public static ShellResult OfAnimated(AnimatedResult a) => new() { Animated = a };
}

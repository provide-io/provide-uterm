//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Session;

public sealed class Cursor
{
    public int X { get; set; }
    public int Y { get; set; }
}

/// <summary>Struct form of cursor position used by some call sites.</summary>
public struct CursorPos
{
    public int X { get; set; }
    public int Y { get; set; }

    public CursorPos(int x, int y)
    {
        X = x;
        Y = y;
    }
}

public sealed class PromptDetection
{
    public string PromptId { get; set; } = "";
    public string InputType { get; set; } = "";
    public bool IsIdle { get; set; }
    public Dictionary<string, object?> KvData { get; set; } = new();
    public Dictionary<string, object?> Extra { get; set; } = new();
}

/// <summary>Emulated screen state. Mirrors TerminalEmulator.GetSnapshot().</summary>
public sealed class Snapshot
{
    public string Screen { get; set; } = "";
    public string ScreenHash { get; set; } = "";
    public Cursor Cursor { get; set; } = new();
    public int Cols { get; set; }
    public int Rows { get; set; }
    public string Term { get; set; } = "ANSI";
    public bool CursorAtEnd { get; set; }
    public bool HasTrailingSpace { get; set; }
    public string RawTail { get; set; } = "";
    public double CapturedAt { get; set; }
    public PromptDetection? PromptDetected { get; set; }
}

/// <summary>Minimal session protocol for PromptWaiter / InputSender.</summary>
public interface ISession
{
    Task<bool> WaitForUpdateAsync(TimeSpan timeout, CancellationToken cancellationToken = default);
    Snapshot Snapshot();
    Task SendAsync(string data, CancellationToken cancellationToken = default);
}

public interface IConnectionChecker
{
    bool IsConnected();
}

public interface IExpectSession
{
    Task SendAsync(string data, CancellationToken cancellationToken = default);
    Snapshot Snapshot();
    int ScreenChangeSeq();
    Task<bool> WaitForScreenChangeAsync(TimeSpan timeout, int since, CancellationToken cancellationToken = default);
}

public sealed class ExpectOptions
{
    public string? ExpectText { get; set; }
    public string? ExpectRegex { get; set; }
    public TimeSpan Timeout { get; set; } = TimeSpan.FromSeconds(10);
}

public sealed class ExpectResult
{
    public bool Matched { get; set; }
    public Snapshot Snapshot { get; set; } = new();
    public string Screen { get; set; } = "";
}

/// <summary>Send keys and wait for expected terminal output.</summary>
public static class Expect
{
    public static async Task<ExpectResult> SendAndExpectAsync(
        IExpectSession session,
        string keys,
        ExpectOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        options ??= new ExpectOptions();
        var since = session.ScreenChangeSeq();
        await session.SendAsync(keys, cancellationToken);
        var deadline = DateTime.UtcNow + options.Timeout;
        while (DateTime.UtcNow < deadline)
        {
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
            {
                break;
            }

            await session.WaitForScreenChangeAsync(remaining, since, cancellationToken);
            var snap = session.Snapshot();
            var screen = snap.Screen;
            var matched = false;
            if (!string.IsNullOrEmpty(options.ExpectText) && screen.Contains(options.ExpectText, StringComparison.Ordinal))
            {
                matched = true;
            }
            else if (!string.IsNullOrEmpty(options.ExpectRegex) &&
                     System.Text.RegularExpressions.Regex.IsMatch(screen, options.ExpectRegex))
            {
                matched = true;
            }
            else if (string.IsNullOrEmpty(options.ExpectText) && string.IsNullOrEmpty(options.ExpectRegex))
            {
                matched = true;
            }

            if (matched)
            {
                return new ExpectResult { Matched = true, Snapshot = snap, Screen = screen };
            }

            since = session.ScreenChangeSeq();
        }

        var final = session.Snapshot();
        return new ExpectResult { Matched = false, Snapshot = final, Screen = final.Screen };
    }
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using Tomlyn.Model;

namespace Provide.Uterm.ServerConfig;

/// <summary>
/// Reading <c>[session_persistence]</c>, the way the reference does
/// (<c>config_schema.SessionPersistenceConfig</c>).
///
/// This port has no capture connector and keeps no capture session, so nothing
/// reads the values. One server.toml is meant to be readable by any port, so
/// the section is still accepted, and a bound the reference refuses is refused
/// here in its words.
/// </summary>
internal static class SessionPersistenceLoader
{
    public static void Apply(SessionPersistenceConfig p, TomlTable t)
    {
        if (t.TryGetValue("persist_capture", out var pc) && pc is bool pcb) p.PersistCapture = pcb;
        if (t.TryGetValue("previous_keep_days", out var kd))
        {
            p.PreviousKeepDays = WithinBounds(
                kd,
                p.PreviousKeepDays,
                SessionPersistenceConfig.MaxPreviousKeepDays,
                "session_persistence.previous_keep_days must be between 0 (keep no previous sessions) and {0}, got: {1}");
        }

        if (t.TryGetValue("previous_scrollback_lines", out var sl))
        {
            p.PreviousScrollbackLines = WithinBounds(
                sl,
                p.PreviousScrollbackLines,
                SessionPersistenceConfig.MaxPreviousScrollbackLines,
                "session_persistence.previous_scrollback_lines must be between 0 and {0}, got: {1}");
        }

        if (t.TryGetValue("state_dir", out var sd) && sd is string dir)
        {
            var normalised = PyPath(dir);
            if (!normalised.StartsWith('/'))
            {
                throw new ArgumentException($"session_persistence.state_dir must be an absolute path, got: {normalised}");
            }

            p.StateDir = normalised;
        }

        if (p.StateDir is null && (p.PersistCapture || p.PreviousKeepDays > 0))
        {
            throw new ArgumentException(
                "session_persistence.state_dir is required when session_persistence.persist_capture "
                + "is on or session_persistence.previous_keep_days is above 0");
        }
    }

    /// <summary>
    /// A path as Python's <c>pathlib.Path</c> writes it: duplicate and trailing
    /// separators and bare <c>.</c> components gone, <c>..</c> kept, and an
    /// empty relative path written <c>.</c>. Refusals quote it so.
    /// </summary>
    private static string PyPath(string path)
    {
        var parts = path.Split('/').Where(part => part.Length > 0 && part != ".");
        var joined = string.Join('/', parts);
        if (path.StartsWith('/'))
        {
            return "/" + joined;
        }

        return joined.Length == 0 ? "." : joined;
    }

    /// <summary>
    /// A count between 0 and <paramref name="max"/>, read as the loader reads
    /// any integer but bounded before it is narrowed, so a huge TOML integer is
    /// refused rather than wrapped into range.
    /// </summary>
    private static int WithinBounds(object? v, int fallback, int max, string message)
    {
        long value = v switch
        {
            long l => l,
            double d => (long)d,
            string s when long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) => n,
            _ => fallback,
        };
        if (value < 0 || value > max)
        {
            throw new ArgumentException(string.Format(CultureInfo.InvariantCulture, message, max, value));
        }

        return (int)value;
    }
}

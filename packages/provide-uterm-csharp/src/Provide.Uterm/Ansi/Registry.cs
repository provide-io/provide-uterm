//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Ansi;

/// <summary>Pluggable color dialect registry.</summary>
public static class ColorDialectRegistry
{
    private static readonly object Gate = new();
    private static readonly List<(string Name, Func<string, string> Handler)> Registry = new();

    static ColorDialectRegistry()
    {
        // Four distinct built-in names into an empty list: nothing to dedupe,
        // so they are added directly rather than through the checked path.
        Registry.Add(("brace_tokens", Dialects.HandleBraceTokens));
        Registry.Add(("extended_tokens", Dialects.HandleExtendedTokens));
        Registry.Add(("tilde_codes", Dialects.HandleTildeCodes));
        Registry.Add(("pipe_codes", Dialects.HandlePipeCodes));
    }

    public static string? RegisterColorDialect(string name, Func<string, string> handler)
    {
        lock (Gate)
        {
            if (Registry.Any(d => d.Name == name))
            {
                return $"color dialect \"{name}\" is already registered";
            }

            Registry.Add((name, handler));
            return null;
        }
    }

    public static string? UnregisterColorDialect(string name)
    {
        lock (Gate)
        {
            var i = Registry.FindIndex(d => d.Name == name);
            if (i < 0)
            {
                return $"color dialect \"{name}\" is not registered";
            }

            Registry.RemoveAt(i);
            return null;
        }
    }

    public static IReadOnlyList<string> RegisteredDialects()
    {
        lock (Gate)
        {
            return Registry.Select(d => d.Name).ToList();
        }
    }

    public static string NormalizeColors(string text)
    {
        List<Func<string, string>> handlers;
        lock (Gate)
        {
            handlers = Registry.Select(d => d.Handler).ToList();
        }

        foreach (var h in handlers)
        {
            text = h(text);
        }

        return text;
    }

    public static string PreviewAnsi(string text) => NormalizeColors(text);
}

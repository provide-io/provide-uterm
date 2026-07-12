//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Detection;

/// <summary>
/// Prompt pattern detector. Port of packages/provide-uterm-go/detection public surface.
/// </summary>
public sealed class Detector
{
    private readonly List<CompiledPattern> _patterns = new();

    private sealed class CompiledPattern
    {
        public required string Id { get; init; }
        public required Regex Regex { get; init; }
        public Regex? NegativeRegex { get; init; }
        public string? NegativeMatch { get; init; }
        public string InputType { get; init; } = "text";
        public string EolPattern { get; init; } = "";
        public Dictionary<string, object?> Pattern { get; init; } = new();
        public object? KvExtract { get; init; }
        public bool ExpectCursorAtEnd { get; init; }
    }

    public void AddPattern(IReadOnlyDictionary<string, object?> pattern)
    {
        var id = pattern.GetValueOrDefault("id") as string ?? "";
        var regexStr = pattern.GetValueOrDefault("regex") as string ?? "";
        if (id.Length == 0 || regexStr.Length == 0)
        {
            throw new ArgumentException("pattern requires id and regex");
        }

        Regex? neg = null;
        if (pattern.GetValueOrDefault("negative_regex") is string nr && nr.Length > 0)
        {
            neg = new Regex(nr, RegexOptions.Compiled | RegexOptions.Multiline);
        }

        _patterns.Add(new CompiledPattern
        {
            Id = id,
            Regex = new Regex(regexStr, RegexOptions.Compiled | RegexOptions.Multiline),
            NegativeRegex = neg,
            NegativeMatch = pattern.GetValueOrDefault("negative_match") as string,
            InputType = pattern.GetValueOrDefault("input_type") as string ?? "text",
            EolPattern = pattern.GetValueOrDefault("eol_pattern") as string ?? "",
            Pattern = pattern is Dictionary<string, object?> d ? d : new Dictionary<string, object?>(pattern),
            KvExtract = pattern.GetValueOrDefault("kv_extract"),
            ExpectCursorAtEnd = pattern.GetValueOrDefault("expect_cursor_at_end") is true,
        });
    }

    public PromptDetection? Detect(IReadOnlyDictionary<string, object?> snapshot)
    {
        var screen = snapshot.GetValueOrDefault("screen") as string ?? "";
        var cursorAtEnd = snapshot.GetValueOrDefault("cursor_at_end") is true;
        foreach (var p in _patterns)
        {
            if (p.ExpectCursorAtEnd && !cursorAtEnd)
            {
                continue;
            }

            if (!p.Regex.IsMatch(screen))
            {
                continue;
            }

            if (p.NegativeRegex is not null && p.NegativeRegex.IsMatch(screen))
            {
                continue;
            }

            if (!string.IsNullOrEmpty(p.NegativeMatch) &&
                screen.Contains(p.NegativeMatch, StringComparison.Ordinal))
            {
                continue;
            }

            return new PromptDetection
            {
                PromptId = p.Id,
                InputType = p.InputType,
                Match = new PromptMatch
                {
                    PromptId = p.Id,
                    Pattern = p.Pattern,
                    InputType = p.InputType,
                    EolPattern = p.EolPattern,
                    KvExtract = p.KvExtract,
                },
                Buffer = new ScreenBuffer
                {
                    Text = screen,
                    Hash = snapshot.GetValueOrDefault("screen_hash") as string ?? "",
                },
            };
        }

        return null;
    }
}

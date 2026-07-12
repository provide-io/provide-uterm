//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Detection;

/// <summary>Screen-snapshot dictionary passed to the detector.</summary>
public sealed class ScreenBuffer
{
    public string Text { get; set; } = "";
    public string Hash { get; set; } = "";
}

public sealed class PromptMatch
{
    public string PromptId { get; set; } = "";
    public Dictionary<string, object?> Pattern { get; set; } = new();
    public string InputType { get; set; } = "";
    public string EolPattern { get; set; } = "";
    public object? KvExtract { get; set; }
}

public sealed class PromptDetection
{
    public string PromptId { get; set; } = "";
    public string InputType { get; set; } = "";
    public Dictionary<string, object?> KvData { get; set; } = new();
    public PromptMatch? Match { get; set; }
    public bool? IsIdle { get; set; }
    public ScreenBuffer? Buffer { get; set; }
}

public sealed class PromptDetectionDiagnostics
{
    public PromptMatch? Match { get; set; }
    public List<Dictionary<string, object?>> RegexMatchedButFailed { get; set; } = new();
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Fanout;

/// <summary>Named set of worker sessions that receive broadcast input together.</summary>
public sealed class Group
{
    public string GroupId { get; set; } = "";
    public string Name { get; set; } = "";
    public List<string> WorkerIds { get; set; } = new();
    public string CreatedBy { get; set; } = "";
    public double CreatedAt { get; set; }
    public string Mode { get; set; } = "parallel";
    public bool StopOnFirstError { get; set; }
    public string ErrorPattern { get; set; } = "";
    public int QuiesceMs { get; set; }
    public int MaxResponseMs { get; set; }
    public double DivergenceThreshold { get; set; } = 0.8;
    public List<string> Grants { get; set; } = new();

    internal Group DeepClone() => new()
    {
        GroupId = GroupId,
        Name = Name,
        WorkerIds = WorkerIds.ToList(),
        CreatedBy = CreatedBy,
        CreatedAt = CreatedAt,
        Mode = Mode,
        StopOnFirstError = StopOnFirstError,
        ErrorPattern = ErrorPattern,
        QuiesceMs = QuiesceMs,
        MaxResponseMs = MaxResponseMs,
        DivergenceThreshold = DivergenceThreshold,
        Grants = Grants.ToList(),
    };
}

public sealed class SessionResult
{
    public string WorkerId { get; set; } = "";
    public bool Ok { get; set; }
    public string? OutputDelta { get; set; }
    public int ElapsedMs { get; set; }
    public bool Divergent { get; set; }

    public Dictionary<string, object?> ToMap() => new()
    {
        ["worker_id"] = WorkerId,
        ["ok"] = Ok,
        ["output_delta"] = OutputDelta,
        ["elapsed_ms"] = ElapsedMs,
        ["divergent"] = Divergent,
    };
}

public sealed class Result
{
    public string GroupId { get; set; } = "";
    public string SendId { get; set; } = "";
    public string Command { get; set; } = "";
    public double SentAt { get; set; }
    public List<SessionResult> Results { get; set; } = new();
    public List<string> DivergentSessions { get; set; } = new();
    public List<string> FailedSessions { get; set; } = new();

    public IReadOnlyList<Dictionary<string, object?>> ResultMaps() =>
        Results.Select(r => r.ToMap()).ToList();
}

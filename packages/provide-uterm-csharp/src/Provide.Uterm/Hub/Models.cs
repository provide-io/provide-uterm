//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Gui;

namespace Provide.Uterm.Hub;

/// <summary>Input modes for a worker. Mirrors provide.uterm.bridge.contracts.InputMode.</summary>
public static class InputModes
{
    public const string Hijack = "hijack";
    public const string Open = "open";
}

/// <summary>Worker-side transport surface used by the lease manager (send_text).</summary>
public interface IWorkerWs
{
    Task SendTextAsync(string payload, CancellationToken cancellationToken = default);
}

/// <summary>Live REST hijack lease. Port of HijackSession.</summary>
public sealed class HijackSession
{
    public required string HijackId { get; set; }
    public required string Owner { get; set; }
    public double LeaseExpiresAt { get; set; }
    public double AcquiredAt { get; set; }
    public double LastHeartbeat { get; set; }
    public string? AcquiredBy { get; set; }
}

/// <summary>View over dashboard-WS and REST hijack fields.</summary>
public sealed class HijackLease
{
    public object? Ws { get; set; }
    public double? WsExpiresAt { get; set; }
    public HijackSession? Session { get; set; }

    public bool IsIdle => Ws is null && Session is null;

    public bool IsDashboardActive(double now) =>
        Ws is not null && WsExpiresAt is { } exp && exp > now;

    public bool IsRestActive(double now) =>
        Session is not null && Session.LeaseExpiresAt > now;

    public bool IsActive(double now) => IsDashboardActive(now) || IsRestActive(now);

    public (bool RestExpired, bool DashExpired) Expire(double now)
    {
        var restExpired = Session is not null && Session.LeaseExpiresAt <= now;
        var dashExpired = Ws is not null && WsExpiresAt is { } exp && exp <= now;
        if (restExpired) Session = null;
        if (dashExpired)
        {
            Ws = null;
            WsExpiresAt = null;
        }

        return (restExpired, dashExpired);
    }
}

/// <summary>Per-worker connection state held by the registry.</summary>
public sealed class WorkerTermState
{
    public IWorkerWs? WorkerWs { get; set; }
    public Dictionary<object, string> Browsers { get; } = new();
    public object? HijackOwner { get; set; }
    public double? HijackOwnerExpiresAt { get; set; }
    public HijackSession? HijackSession { get; set; }
    public string? HijackPending { get; set; }
    public string InputMode { get; set; } = InputModes.Hijack;
    public Dictionary<string, object?>? LastSnapshot { get; set; }
    public List<Dictionary<string, object?>> Events { get; } = new();
    public int EventSeq { get; set; }
    public int MinEventSeq { get; set; }
    public double LastActivityAt { get; set; }
    public int? ProtocolVersion { get; set; }
    public bool IsTunnelWorker { get; set; }

    /// <summary>Optional remote GUI session (memory fixture or RFB client).</summary>
    public IGraphicalSession? GraphicalSession { get; set; }

    public HijackLease Lease() => new()
    {
        Ws = HijackOwner,
        WsExpiresAt = HijackOwnerExpiresAt,
        Session = HijackSession,
    };

    public void ApplyLease(HijackLease l)
    {
        HijackOwner = l.Ws;
        HijackOwnerExpiresAt = l.WsExpiresAt;
        HijackSession = l.Session;
    }
}

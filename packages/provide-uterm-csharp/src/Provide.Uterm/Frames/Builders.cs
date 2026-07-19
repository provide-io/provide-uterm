//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Frames;

/// <summary>Builder helpers mirroring Python/Go frame builders.</summary>
public static class FrameBuilders
{
    private static double NowTs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    private static double TsOrNow(double ts) => ts > 0 ? ts : NowTs();

    public static ErrorFrame MakeErrorFrame(string message) =>
        new() { Type = FrameTypeNames.Error, Message = message };

    public static PongFrame MakePongFrame(double ts = 0) =>
        new() { Type = FrameTypeNames.Pong, Ts = TsOrNow(ts) };

    public static HeartbeatAckFrame MakeHeartbeatAckFrame(double leaseExpiresAt, double ts = 0) =>
        new() { Type = FrameTypeNames.HeartbeatAck, LeaseExpiresAt = leaseExpiresAt, Ts = TsOrNow(ts) };

    public static WorkerConnectedFrame MakeWorkerConnectedFrame(string workerId, double ts = 0) =>
        new() { Type = FrameTypeNames.WorkerConnected, WorkerId = workerId, Ts = TsOrNow(ts) };

    public static WorkerDisconnectedFrame MakeWorkerDisconnectedFrame(string workerId, double ts = 0) =>
        new() { Type = FrameTypeNames.WorkerDisconnected, WorkerId = workerId, Ts = TsOrNow(ts) };

    public static TermFrame MakeTermFrame(string data, double ts = 0) =>
        new() { Type = FrameTypeNames.Term, Data = data, Ts = TsOrNow(ts) };

    public sealed class SnapshotParams
    {
        public string Screen { get; set; } = "";
        public Dictionary<string, int>? Cursor { get; set; }
        public int Cols { get; set; }
        public int Rows { get; set; }
        public string ScreenHash { get; set; } = "";
        public bool CursorAtEnd { get; set; }
        public bool HasTrailingSpace { get; set; }
        public Dictionary<string, object?>? PromptDetected { get; set; }
        public double Ts { get; set; }
        public string? RawTail { get; set; }
    }

    public static SnapshotFrame MakeSnapshotFrame(SnapshotParams p) =>
        new()
        {
            Type = FrameTypeNames.Snapshot,
            Screen = p.Screen,
            Cursor = p.Cursor,
            Cols = p.Cols,
            Rows = p.Rows,
            ScreenHash = p.ScreenHash,
            CursorAtEnd = p.CursorAtEnd,
            HasTrailingSpace = p.HasTrailingSpace,
            PromptDetected = p.PromptDetected,
            RawTail = p.RawTail,
            Ts = TsOrNow(p.Ts),
        };

    public static AnalysisFrame MakeAnalysisFrame(string formatted, object? raw = null, double ts = 0) =>
        new() { Type = FrameTypeNames.Analysis, Formatted = formatted, Raw = raw, Ts = TsOrNow(ts) };

    public static HijackStateFrame MakeHijackStateFrame(bool hijacked, string? owner, double? leaseExpiresAt, string inputMode) =>
        new()
        {
            Type = FrameTypeNames.HijackState,
            Hijacked = hijacked,
            Owner = owner,
            LeaseExpiresAt = leaseExpiresAt,
            InputMode = inputMode,
        };

    /// <summary>Type stamp only (matches Python/Go golden builders).</summary>
    public static HelloFrame MakeHelloFrame() => new() { Type = FrameTypeNames.Hello };

    /// <summary>C# hello defaults from spec/behavior.json (mcp=false, vnc=true).</summary>
    public static HelloFrame MakeHelloFrameWithDefaults() => new()
    {
        Type = FrameTypeNames.Hello,
        McpSupported = false,
        VncSupported = true,
    };

    public static IdentityFrame NewIdentityFrame(string subject) =>
        new()
        {
            Type = FrameTypeNames.Identity,
            Version = FrameTypeNames.IdentityDefaultVersion,
            Subject = subject,
            Fingerprint = FrameTypeNames.IdentityDefaultFingerprint,
            Transport = FrameTypeNames.IdentityDefaultTransport,
        };

    public static StatusFrame CoerceWorkerStatusFrame(IReadOnlyDictionary<string, object?> payload)
    {
        var f = new StatusFrame { Type = FrameTypeNames.Status };
        var hasTs = false;
        foreach (var (k, v) in payload)
        {
            switch (k)
            {
                case "type":
                    if (v is string s)
                    {
                        f.Type = s;
                        continue;
                    }

                    break;
                case "ts":
                    hasTs = true;
                    switch (v)
                    {
                        case double d:
                            f.Ts = d;
                            continue;
                        case float fl:
                            f.Ts = fl;
                            continue;
                        case int i:
                            f.Ts = i;
                            continue;
                        case long l:
                            f.Ts = l;
                            continue;
                    }

                    break;
            }

            f.Extra ??= new Dictionary<string, object?>();
            f.Extra[k] = v;
        }

        if (!hasTs)
        {
            f.Ts = NowTs();
        }

        return f;
    }
}

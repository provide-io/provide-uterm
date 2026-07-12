//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text.Json;

namespace Provide.Uterm.Frames;

/// <summary>Maps between typed frames and wire dictionaries (exclude_none=True).</summary>
internal static class FrameMapper
{
    public static Dictionary<string, object?> ToDict(IFrame frame) => frame switch
    {
        TermFrame f => Dict(("type", f.Type), ("data", f.Data), Opt("ts", f.Ts)),
        InputFrame f => Dict(("type", f.Type), ("data", f.Data), Opt("ts", f.Ts)),
        SnapshotReqFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        SnapshotFrame f => Dict(
            ("type", f.Type), ("screen", f.Screen),
            Opt("cursor", f.Cursor), Opt("cols", f.Cols), Opt("rows", f.Rows),
            Opt("screen_hash", f.ScreenHash), Opt("cursor_at_end", f.CursorAtEnd),
            Opt("has_trailing_space", f.HasTrailingSpace), Opt("prompt_detected", f.PromptDetected),
            Opt("raw_tail", f.RawTail), Opt("ts", f.Ts)),
        ControlFrame f => Dict(("type", f.Type), ("action", f.Action), Opt("owner", f.Owner), Opt("lease_s", f.LeaseS), Opt("ts", f.Ts)),
        HijackStateFrame f => Dict(("type", f.Type), ("hijacked", f.Hijacked), Opt("owner", f.Owner), Opt("lease_expires_at", f.LeaseExpiresAt), Opt("input_mode", f.InputMode)),
        HijackRequestFrame f => Dict(("type", f.Type), Opt("token", f.Token), Opt("ts", f.Ts)),
        HijackReleaseFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        HijackStepFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        WorkerConnectedFrame f => Dict(("type", f.Type), ("worker_id", f.WorkerId), Opt("ts", f.Ts)),
        WorkerDisconnectedFrame f => Dict(("type", f.Type), ("worker_id", f.WorkerId), Opt("ts", f.Ts)),
        WorkerHelloFrame f => Dict(("type", f.Type), Opt("mode", f.Mode), Opt("ts", f.Ts)),
        HeartbeatFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        HeartbeatAckFrame f => Dict(("type", f.Type), ("lease_expires_at", f.LeaseExpiresAt), Opt("ts", f.Ts)),
        PingFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        PongFrame f => Dict(("type", f.Type), Opt("ts", f.Ts)),
        HelloFrame f => Dict(
            ("type", f.Type), Opt("worker_id", f.WorkerId), Opt("can_hijack", f.CanHijack),
            Opt("hijacked", f.Hijacked), Opt("hijacked_by_me", f.HijackedByMe), Opt("worker_online", f.WorkerOnline),
            Opt("input_mode", f.InputMode), Opt("role", f.Role), Opt("hijack_control", f.HijackControl),
            Opt("hijack_step_supported", f.HijackStepSupported), Opt("capabilities", f.Capabilities),
            Opt("resume_supported", f.ResumeSupported), Opt("resume_token", f.ResumeToken),
            Opt("resumed", f.Resumed), Opt("protocol_version", f.ProtocolVersion),
            Opt("protocol", f.Protocol), Opt("ts", f.Ts)),
        ResumeFrame f => Dict(("type", f.Type), ("token", f.Token), Opt("player_id", f.PlayerId)),
        IdentityFrame f => MergeExtra(Dict(
            ("type", f.Type), ("version", f.Version), ("subject", f.Subject),
            ("fingerprint", f.Fingerprint), ("transport", f.Transport),
            Opt("claims", f.Claims), Opt("signature", f.Signature)), f.Extra),
        SessionTokenFrame f => Dict(("type", f.Type), ("token", f.Token), Opt("player_id", f.PlayerId)),
        ResumeOkFrame f => Dict(("type", f.Type)),
        ResumeFailedFrame f => Dict(("type", f.Type), Opt("reason", f.Reason)),
        LinkPatternsFrame f => Dict(("type", f.Type), ("patterns", f.Patterns.Select(LinkToDict).ToList())),
        AnalysisFrame f => Dict(("type", f.Type), ("formatted", f.Formatted), Opt("raw", f.Raw), Opt("ts", f.Ts)),
        ErrorFrame f => Dict(("type", f.Type), ("message", f.Message), Opt("reason", f.Reason),
            Opt("client_min", f.ClientMin), Opt("client_max", f.ClientMax),
            Opt("server_min", f.ServerMin), Opt("server_max", f.ServerMax)),
        StatusFrame f => MergeExtra(Dict(("type", f.Type), Opt("ts", f.Ts)), f.Extra),
        InputModeChangedFrame f => Dict(("type", f.Type), ("input_mode", f.InputMode), Opt("ts", f.Ts)),
        ApprovalPendingFrame f => Dict(("type", f.Type), ("command", f.Command), ("request_id", f.RequestId), ("expires_at", f.ExpiresAt)),
        ApprovalResolvedFrame f => Dict(("type", f.Type), ("outcome", f.Outcome), ("request_id", f.RequestId)),
        PresenceUpdateFrame f => MergeExtra(Dict(("type", f.Type), Opt("user_id", f.UserId)), f.Extra),
        PresenceSyncFrame f => MergeExtra(Dict(("type", f.Type), Opt("users", f.Users), Opt("config", f.Config), Opt("owner_id", f.OwnerId)), f.Extra),
        PresenceLeaveFrame f => Dict(("type", f.Type), ("user_id", f.UserId), Opt("ts", f.Ts)),
        ControlTransferFrame f => Dict(("type", f.Type), Opt("from_user_id", f.FromUserId), Opt("to_user_id", f.ToUserId), Opt("reason", f.Reason), Opt("queued_keys", f.QueuedKeys)),
        _ => throw new ArgumentException($"frames: {frame.GetType().Name} is not a frame struct"),
    };

    private static Dictionary<string, object?> LinkToDict(LinkPatternEntry e) =>
        Dict(("pattern", e.Pattern), ("action", e.Action),
            Opt("id", e.Id), Opt("flags", e.Flags), Opt("group", e.Group),
            Opt("payload", e.Payload), Opt("hover", e.Hover),
            Opt("line_contains", e.LineContains), Opt("class", e.Class));

    public static IFrame FromJson(JsonElement root, string type)
    {
        string? S(string k) => root.TryGetProperty(k, out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        double? D(string k)
        {
            if (!root.TryGetProperty(k, out var e) || e.ValueKind == JsonValueKind.Null)
            {
                return null;
            }

            return e.ValueKind == JsonValueKind.Number ? e.GetDouble() : null;
        }

        int? I(string k)
        {
            if (!root.TryGetProperty(k, out var e) || e.ValueKind == JsonValueKind.Null)
            {
                return null;
            }

            return e.ValueKind == JsonValueKind.Number ? e.GetInt32() : null;
        }

        bool? B(string k)
        {
            if (!root.TryGetProperty(k, out var e) || e.ValueKind == JsonValueKind.Null)
            {
                return null;
            }

            return e.ValueKind is JsonValueKind.True or JsonValueKind.False ? e.GetBoolean() : null;
        }

        Dictionary<string, object?>? Obj(string k) =>
            root.TryGetProperty(k, out var e) && e.ValueKind == JsonValueKind.Object
                ? (Dictionary<string, object?>)FrameCodec.JsonToObject(e)!
                : null;

        return type switch
        {
            FrameTypeNames.Term => new TermFrame { Type = type, Data = S("data") ?? "", Ts = D("ts") },
            FrameTypeNames.Input => new InputFrame { Type = type, Data = S("data") ?? "", Ts = D("ts") },
            FrameTypeNames.SnapshotReq => new SnapshotReqFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.Snapshot => new SnapshotFrame
            {
                Type = type,
                Screen = S("screen") ?? "",
                Cursor = root.TryGetProperty("cursor", out var cur) && cur.ValueKind == JsonValueKind.Object
                    ? cur.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetInt32())
                    : null,
                Cols = I("cols"),
                Rows = I("rows"),
                ScreenHash = S("screen_hash"),
                CursorAtEnd = B("cursor_at_end"),
                HasTrailingSpace = B("has_trailing_space"),
                PromptDetected = Obj("prompt_detected"),
                RawTail = S("raw_tail"),
                Ts = D("ts"),
            },
            FrameTypeNames.Control => new ControlFrame { Type = type, Action = S("action") ?? "", Owner = S("owner"), LeaseS = D("lease_s"), Ts = D("ts") },
            FrameTypeNames.HijackState => new HijackStateFrame
            {
                Type = type,
                Hijacked = root.TryGetProperty("hijacked", out var hj) && hj.ValueKind == JsonValueKind.True,
                Owner = S("owner"),
                LeaseExpiresAt = D("lease_expires_at"),
                InputMode = S("input_mode"),
            },
            FrameTypeNames.HijackRequest => new HijackRequestFrame { Type = type, Token = S("token"), Ts = D("ts") },
            FrameTypeNames.HijackRelease => new HijackReleaseFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.HijackStep => new HijackStepFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.WorkerConnected => new WorkerConnectedFrame { Type = type, WorkerId = S("worker_id") ?? "", Ts = D("ts") },
            FrameTypeNames.WorkerDisconnected => new WorkerDisconnectedFrame { Type = type, WorkerId = S("worker_id") ?? "", Ts = D("ts") },
            FrameTypeNames.WorkerHello => new WorkerHelloFrame { Type = type, Mode = S("mode"), Ts = D("ts") },
            FrameTypeNames.Heartbeat => new HeartbeatFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.HeartbeatAck => new HeartbeatAckFrame { Type = type, LeaseExpiresAt = D("lease_expires_at") ?? 0, Ts = D("ts") },
            FrameTypeNames.Ping => new PingFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.Pong => new PongFrame { Type = type, Ts = D("ts") },
            FrameTypeNames.Hello => DecodeHello(root, type, S, B, I, D, Obj),
            FrameTypeNames.Resume => new ResumeFrame { Type = type, Token = S("token") ?? "", PlayerId = I("player_id") },
            FrameTypeNames.Identity => DecodeIdentity(root, type, S, I),
            FrameTypeNames.SessionToken => new SessionTokenFrame { Type = type, Token = S("token") ?? "", PlayerId = I("player_id") },
            FrameTypeNames.ResumeOk => new ResumeOkFrame { Type = type },
            FrameTypeNames.ResumeFailed => new ResumeFailedFrame { Type = type, Reason = S("reason") },
            FrameTypeNames.LinkPatterns => DecodeLinkPatterns(root, type),
            FrameTypeNames.Analysis => new AnalysisFrame
            {
                Type = type,
                Formatted = S("formatted") ?? "",
                Raw = root.TryGetProperty("raw", out var raw) ? FrameCodec.JsonToObject(raw) : null,
                Ts = D("ts"),
            },
            FrameTypeNames.Error => new ErrorFrame
            {
                Type = type, Message = S("message") ?? "", Reason = S("reason"),
                ClientMin = I("client_min"), ClientMax = I("client_max"),
                ServerMin = I("server_min"), ServerMax = I("server_max"),
            },
            FrameTypeNames.Status => DecodeStatus(root, type, D),
            FrameTypeNames.InputModeChanged => new InputModeChangedFrame { Type = type, InputMode = S("input_mode") ?? "", Ts = D("ts") },
            FrameTypeNames.ApprovalPending => new ApprovalPendingFrame
            {
                Type = type, Command = S("command") ?? "", RequestId = S("request_id") ?? "",
                ExpiresAt = D("expires_at") ?? 0,
            },
            FrameTypeNames.ApprovalResolved => new ApprovalResolvedFrame { Type = type, Outcome = S("outcome") ?? "", RequestId = S("request_id") ?? "" },
            FrameTypeNames.PresenceUpdate => DecodePresenceUpdate(root, type, S),
            FrameTypeNames.PresenceSync => DecodePresenceSync(root, type, S, Obj),
            FrameTypeNames.PresenceLeave => new PresenceLeaveFrame { Type = type, UserId = S("user_id") ?? "", Ts = D("ts") },
            FrameTypeNames.ControlTransfer => new ControlTransferFrame
            {
                Type = type, FromUserId = S("from_user_id"), ToUserId = S("to_user_id"),
                Reason = S("reason"), QueuedKeys = S("queued_keys"),
            },
            _ => throw new ArgumentException($"frames: unknown frame type \"{type}\""),
        };
    }

    private static HelloFrame DecodeHello(JsonElement root, string type,
        Func<string, string?> S, Func<string, bool?> B, Func<string, int?> I, Func<string, double?> D,
        Func<string, Dictionary<string, object?>?> Obj)
    {
        Dictionary<string, int>? protocol = null;
        if (root.TryGetProperty("protocol", out var p) && p.ValueKind == JsonValueKind.Object)
        {
            protocol = p.EnumerateObject().ToDictionary(x => x.Name, x => x.Value.GetInt32());
        }

        return new HelloFrame
        {
            Type = type,
            WorkerId = S("worker_id"),
            CanHijack = B("can_hijack"),
            Hijacked = B("hijacked"),
            HijackedByMe = B("hijacked_by_me"),
            WorkerOnline = B("worker_online"),
            InputMode = S("input_mode"),
            Role = S("role"),
            HijackControl = S("hijack_control"),
            HijackStepSupported = B("hijack_step_supported"),
            Capabilities = Obj("capabilities"),
            ResumeSupported = B("resume_supported"),
            ResumeToken = S("resume_token"),
            Resumed = B("resumed"),
            ProtocolVersion = I("protocol_version"),
            Protocol = protocol,
            Ts = D("ts"),
        };
    }

    private static IdentityFrame DecodeIdentity(JsonElement root, string type, Func<string, string?> S, Func<string, int?> I)
    {
        var known = new HashSet<string> { "type", "version", "subject", "fingerprint", "transport", "claims", "signature" };
        Dictionary<string, object?>? claims = null;
        if (root.TryGetProperty("claims", out var c) && c.ValueKind == JsonValueKind.Object)
        {
            claims = (Dictionary<string, object?>)FrameCodec.JsonToObject(c)!;
        }

        return new IdentityFrame
        {
            Type = type,
            Version = I("version") ?? FrameTypeNames.IdentityDefaultVersion,
            Subject = S("subject") ?? "",
            Fingerprint = root.TryGetProperty("fingerprint", out _) ? (S("fingerprint") ?? "") : FrameTypeNames.IdentityDefaultFingerprint,
            Transport = root.TryGetProperty("transport", out _) ? (S("transport") ?? FrameTypeNames.IdentityDefaultTransport) : FrameTypeNames.IdentityDefaultTransport,
            Claims = claims,
            Signature = S("signature"),
            Extra = ExtractExtra(root, known),
        };
    }

    private static StatusFrame DecodeStatus(JsonElement root, string type, Func<string, double?> D) =>
        new()
        {
            Type = type,
            Ts = D("ts"),
            Extra = ExtractExtra(root, ["type", "ts"]),
        };

    private static PresenceUpdateFrame DecodePresenceUpdate(JsonElement root, string type, Func<string, string?> S) =>
        new()
        {
            Type = type,
            UserId = S("user_id"),
            Extra = ExtractExtra(root, ["type", "user_id"]),
        };

    private static PresenceSyncFrame DecodePresenceSync(JsonElement root, string type, Func<string, string?> S, Func<string, Dictionary<string, object?>?> Obj)
    {
        List<Dictionary<string, object?>>? users = null;
        if (root.TryGetProperty("users", out var u) && u.ValueKind == JsonValueKind.Array)
        {
            users = u.EnumerateArray()
                .Where(e => e.ValueKind == JsonValueKind.Object)
                .Select(e => (Dictionary<string, object?>)FrameCodec.JsonToObject(e)!)
                .ToList();
        }

        return new PresenceSyncFrame
        {
            Type = type,
            Users = users,
            Config = Obj("config"),
            OwnerId = S("owner_id"),
            Extra = ExtractExtra(root, ["type", "users", "config", "owner_id"]),
        };
    }

    private static LinkPatternsFrame DecodeLinkPatterns(JsonElement root, string type)
    {
        var patterns = new List<LinkPatternEntry>();
        if (root.TryGetProperty("patterns", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var e in arr.EnumerateArray())
            {
                string? PS(string k) => e.TryGetProperty(k, out var x) && x.ValueKind == JsonValueKind.String ? x.GetString() : null;
                object? Any(string k) => e.TryGetProperty(k, out var x) ? FrameCodec.JsonToObject(x) : null;
                patterns.Add(new LinkPatternEntry
                {
                    Pattern = PS("pattern") ?? "",
                    Action = PS("action") ?? "",
                    Id = PS("id"),
                    Flags = PS("flags"),
                    Group = Any("group"),
                    Payload = Any("payload"),
                    Hover = PS("hover"),
                    LineContains = PS("line_contains"),
                    Class = PS("class"),
                });
            }
        }

        return new LinkPatternsFrame { Type = type, Patterns = patterns };
    }

    private static Dictionary<string, object?>? ExtractExtra(JsonElement root, IEnumerable<string> known)
    {
        var set = known as HashSet<string> ?? known.ToHashSet(StringComparer.Ordinal);
        Dictionary<string, object?>? extra = null;
        foreach (var prop in root.EnumerateObject())
        {
            if (set.Contains(prop.Name))
            {
                continue;
            }

            extra ??= new Dictionary<string, object?>();
            extra[prop.Name] = FrameCodec.JsonToObject(prop.Value);
        }

        return extra;
    }

    private static (string Key, object? Value)? Opt(string key, object? value) =>
        value is null ? null : (key, value);

    private static Dictionary<string, object?> Dict(params (string Key, object? Value)?[] entries)
    {
        var d = new Dictionary<string, object?>();
        foreach (var e in entries)
        {
            if (e is null)
            {
                continue;
            }

            d[e.Value.Key] = e.Value.Value;
        }

        return d;
    }

    private static Dictionary<string, object?> MergeExtra(Dictionary<string, object?> known, Dictionary<string, object?>? extra)
    {
        if (extra is null || extra.Count == 0)
        {
            return known;
        }

        var outMap = new Dictionary<string, object?>(extra);
        foreach (var (k, v) in known)
        {
            outMap[k] = v;
        }

        return outMap;
    }
}

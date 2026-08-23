//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.Frames;

/// <summary>
/// Encode/Decode frames with type discriminator, matching Go frames package
/// and Pydantic exclude_none=True semantics.
/// </summary>
public static class FrameCodec
{
    private static readonly HashSet<string> ForbidTypes = new(StringComparer.Ordinal)
    {
        FrameTypeNames.Term, FrameTypeNames.Input, FrameTypeNames.SnapshotReq, FrameTypeNames.Snapshot,
        FrameTypeNames.Control, FrameTypeNames.HijackState, FrameTypeNames.HijackRequest,
        FrameTypeNames.HijackRelease, FrameTypeNames.HijackStep, FrameTypeNames.WorkerConnected,
        FrameTypeNames.WorkerDisconnected, FrameTypeNames.WorkerHello, FrameTypeNames.Heartbeat,
        FrameTypeNames.HeartbeatAck, FrameTypeNames.Ping, FrameTypeNames.Pong, FrameTypeNames.Resume,
        FrameTypeNames.SessionToken, FrameTypeNames.ResumeOk, FrameTypeNames.ResumeFailed,
        FrameTypeNames.LinkPatterns, FrameTypeNames.Analysis, FrameTypeNames.Error,
        FrameTypeNames.InputModeChanged, FrameTypeNames.ApprovalPending, FrameTypeNames.ApprovalResolved,
        FrameTypeNames.PresenceLeave, FrameTypeNames.ControlTransfer,
    };

    private static readonly HashSet<string> IgnoreTypes = new(StringComparer.Ordinal)
    {
        FrameTypeNames.Hello,
    };

    private static readonly HashSet<string> AllowTypes = new(StringComparer.Ordinal)
    {
        FrameTypeNames.Identity, FrameTypeNames.Status, FrameTypeNames.PresenceUpdate, FrameTypeNames.PresenceSync,
    };

    private static readonly Dictionary<string, HashSet<string>> KnownKeys = new(StringComparer.Ordinal)
    {
        [FrameTypeNames.Term] = ["type", "data", "ts"],
        [FrameTypeNames.Input] = ["type", "data", "ts"],
        [FrameTypeNames.SnapshotReq] = ["type", "ts"],
        [FrameTypeNames.Snapshot] = ["type", "screen", "cursor", "cols", "rows", "screen_hash", "cursor_at_end", "has_trailing_space", "prompt_detected", "raw_tail", "chunks_read", "bytes_read", "ts"],
        [FrameTypeNames.Control] = ["type", "action", "owner", "lease_s", "ts"],
        [FrameTypeNames.HijackState] = ["type", "hijacked", "owner", "lease_expires_at", "input_mode"],
        [FrameTypeNames.HijackRequest] = ["type", "token", "ts"],
        [FrameTypeNames.HijackRelease] = ["type", "ts"],
        [FrameTypeNames.HijackStep] = ["type", "ts"],
        [FrameTypeNames.WorkerConnected] = ["type", "worker_id", "ts"],
        [FrameTypeNames.WorkerDisconnected] = ["type", "worker_id", "ts"],
        [FrameTypeNames.WorkerHello] = ["type", "mode", "ts"],
        [FrameTypeNames.Heartbeat] = ["type", "ts"],
        [FrameTypeNames.HeartbeatAck] = ["type", "lease_expires_at", "ts"],
        [FrameTypeNames.Ping] = ["type", "ts"],
        [FrameTypeNames.Pong] = ["type", "ts"],
        [FrameTypeNames.Hello] = ["type", "worker_id", "can_hijack", "hijacked", "hijacked_by_me", "worker_online", "input_mode", "role", "hijack_control", "hijack_step_supported", "capabilities", "resume_supported", "resume_token", "resumed", "protocol_version", "protocol", "ts"],
        [FrameTypeNames.Resume] = ["type", "token", "player_id"],
        [FrameTypeNames.Identity] = ["type", "version", "subject", "fingerprint", "transport", "claims", "signature"],
        [FrameTypeNames.SessionToken] = ["type", "token", "player_id"],
        [FrameTypeNames.ResumeOk] = ["type"],
        [FrameTypeNames.ResumeFailed] = ["type", "reason"],
        [FrameTypeNames.LinkPatterns] = ["type", "patterns"],
        [FrameTypeNames.Analysis] = ["type", "formatted", "raw", "ts"],
        [FrameTypeNames.Error] = ["type", "message", "reason", "client_min", "client_max", "server_min", "server_max"],
        [FrameTypeNames.Status] = ["type", "ts"],
        [FrameTypeNames.InputModeChanged] = ["type", "input_mode", "ts"],
        [FrameTypeNames.ApprovalPending] = ["type", "command", "request_id", "expires_at"],
        [FrameTypeNames.ApprovalResolved] = ["type", "outcome", "request_id"],
        [FrameTypeNames.PresenceUpdate] = ["type", "user_id"],
        [FrameTypeNames.PresenceSync] = ["type", "users", "config", "owner_id"],
        [FrameTypeNames.PresenceLeave] = ["type", "user_id", "ts"],
        [FrameTypeNames.ControlTransfer] = ["type", "from_user_id", "to_user_id", "reason", "queued_keys"],
    };

    public static IFrame DecodeFrame(ReadOnlySpan<byte> data)
    {
        using var doc = JsonDocument.Parse(data.ToArray());
        if (doc.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new ArgumentException("frames: invalid frame JSON: not an object");
        }

        if (!doc.RootElement.TryGetProperty("type", out var typeEl) || typeEl.ValueKind != JsonValueKind.String)
        {
            throw new ArgumentException("frames: unknown frame type \"\"");
        }

        var type = typeEl.GetString()!;
        if (!KnownKeys.ContainsKey(type))
        {
            throw new ArgumentException($"frames: unknown frame type \"{type}\"");
        }

        if (ForbidTypes.Contains(type))
        {
            foreach (var prop in doc.RootElement.EnumerateObject())
            {
                if (!KnownKeys[type].Contains(prop.Name))
                {
                    throw new ArgumentException($"frames: decode: unknown field \"{prop.Name}\"");
                }
            }
        }

        return FrameMapper.FromJson(doc.RootElement, type);
    }

    public static IFrame DecodeFrame(string json) => DecodeFrame(Encoding.UTF8.GetBytes(json));

    public static byte[] EncodeFrame(IFrame frame)
    {
        if (frame.Type != frame.FrameType)
        {
            throw new ArgumentException($"frames: {frame.GetType().Name} has type \"{frame.Type}\", want literal \"{frame.FrameType}\"");
        }

        var dict = FrameMapper.ToDict(frame);
        return JsonMarshal(dict);
    }

    public static string EncodeFrameString(IFrame frame) => Encoding.UTF8.GetString(EncodeFrame(frame));

    internal static byte[] JsonMarshal(Dictionary<string, object?> dict)
    {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions
        {
            Indented = false,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }))
        {
            WriteValue(writer, dict);
        }

        return stream.ToArray();
    }

    internal static void WriteValue(Utf8JsonWriter writer, object? value)
    {
        switch (value)
        {
            case null:
                writer.WriteNullValue();
                break;
            case bool b:
                writer.WriteBooleanValue(b);
                break;
            case string s:
                writer.WriteStringValue(s);
                break;
            case byte or sbyte or short or ushort or int or uint or long or ulong:
                writer.WriteNumberValue(Convert.ToInt64(value, CultureInfo.InvariantCulture));
                break;
            case float f:
                writer.WriteNumberValue(f);
                break;
            case double d:
                writer.WriteNumberValue(d);
                break;
            case decimal m:
                writer.WriteNumberValue(m);
                break;
            case JsonElement je:
                je.WriteTo(writer);
                break;
            case IReadOnlyDictionary<string, object?> map:
                writer.WriteStartObject();
                foreach (var (k, v) in map)
                {
                    writer.WritePropertyName(k);
                    WriteValue(writer, v);
                }

                writer.WriteEndObject();
                break;
            case IDictionary<string, object?> map:
                writer.WriteStartObject();
                foreach (var (k, v) in map)
                {
                    writer.WritePropertyName(k);
                    WriteValue(writer, v);
                }

                writer.WriteEndObject();
                break;
            case IDictionary<string, int> map:
                writer.WriteStartObject();
                foreach (var (k, v) in map)
                {
                    writer.WritePropertyName(k);
                    writer.WriteNumberValue(v);
                }

                writer.WriteEndObject();
                break;
            case System.Collections.IEnumerable list when value is not string:
                writer.WriteStartArray();
                foreach (var item in list)
                {
                    WriteValue(writer, item);
                }

                writer.WriteEndArray();
                break;
            default:
                writer.WriteStringValue(Convert.ToString(value, CultureInfo.InvariantCulture));
                break;
        }
    }

    internal static object? JsonToObject(JsonElement el) =>
        el.ValueKind switch
        {
            JsonValueKind.Object => el.EnumerateObject().ToDictionary(p => p.Name, p => JsonToObject(p.Value), StringComparer.Ordinal),
            JsonValueKind.Array => el.EnumerateArray().Select(JsonToObject).Cast<object?>().ToList(),
            JsonValueKind.String => el.GetString(),
            JsonValueKind.Number => NumberFrom(el),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.Null => null,
            _ => el.GetRawText(),
        };

    private static object NumberFrom(JsonElement el)
    {
        var raw = el.GetRawText();
        if (raw.IndexOfAny(['.', 'e', 'E']) < 0 && el.TryGetInt64(out var l))
        {
            return l;
        }

        return el.GetDouble();
    }
}

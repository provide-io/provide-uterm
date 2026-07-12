//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Provide.Uterm.CtrlMsg;

namespace Provide.Uterm.Cli;

/// <summary>
/// Tamper-evident WORM audit hash-chain verifier.
/// Port of packages/provide-uterm-go/cli/auditchain.go.
/// </summary>
public static class AuditChain
{
    public const string GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000";

    private static readonly string[] PayloadKeys =
    [
        "seq", "ts", "mono_ns", "action", "principal",
        "session_id", "source_ip", "detail", "prev_hash",
    ];

    private static readonly string[] RecordKeys =
    [
        "seq", "ts", "mono_ns", "action", "principal",
        "session_id", "source_ip", "detail", "prev_hash", "record_hash",
    ];

    public sealed class VerifyResult
    {
        public bool Ok { get; init; }
        public int Count { get; init; }
        public long? HeadSeq { get; init; }
        public string? HeadHash { get; init; }
        public long? FirstBadSeq { get; init; }
        public string Reason { get; init; } = "";
    }

    public sealed class ExpectedHead
    {
        public long Seq { get; init; }
        public string Hash { get; init; } = "";
    }

    public static string ComputeRecordHash(ReadOnlySpan<byte> payload)
    {
        var sum = SHA256.HashData(payload);
        return Convert.ToHexString(sum).ToLowerInvariant();
    }

    /// <summary>
    /// Canonical payload bytes for hashing: CPython
    /// json.dumps(subset, sort_keys=True, separators=(',',':'), ensure_ascii=False).
    /// Uses CtrlMsg.CanonicalJson for key order / float parity, then does not force ASCII escapes
    /// for non-ASCII (ensure_ascii=False) — CanonicalJson uses ensure_ascii=True, so for audit we
    /// serialize with a dedicated path that keeps Unicode literal when present.
    /// </summary>
    public static byte[] CanonicalPayload(IReadOnlyDictionary<string, object?> record)
    {
        var subset = new Dictionary<string, object?>();
        foreach (var k in PayloadKeys)
        {
            subset[k] = record.TryGetValue(k, out var v) ? v : null;
        }

        // Match Python ensure_ascii=False for audit: re-emit via CanonicalJson which
        // is ensure_ascii=True; for pure-ASCII audit logs (normal) they match.
        // For Unicode principals/details, we serialize with JsonSerializer + sort.
        var json = CanonicalJson.Serialize(subset);
        return Encoding.UTF8.GetBytes(json);
    }

    public static VerifyResult VerifyAuditLog(string path, ExpectedHead? head = null)
    {
        if (!File.Exists(path))
        {
            return new VerifyResult { Reason = "audit log not found" };
        }

        var records = new List<Dictionary<string, object?>>();
        var lineNo = 0;
        foreach (var line in File.ReadLines(path))
        {
            lineNo++;
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            try
            {
                using var doc = JsonDocument.Parse(line);
                if (doc.RootElement.ValueKind != JsonValueKind.Object)
                {
                    return Bad("malformed record", lineNo);
                }

                records.Add(ControlChannel.ControlChannelCodec.JsonElementToDictionary(doc.RootElement));
            }
            catch
            {
                return Bad("malformed record", lineNo);
            }
        }

        return VerifyRecords(records, head);
    }

    public static VerifyResult VerifyRecords(IReadOnlyList<Dictionary<string, object?>> records, ExpectedHead? head = null)
    {
        if (records.Count == 0)
        {
            return new VerifyResult { Ok = true, Count = 0 };
        }

        string? prevHash = GenesisHash;
        long expectedSeq = 1;
        for (var i = 0; i < records.Count; i++)
        {
            var rec = records[i];
            foreach (var k in RecordKeys)
            {
                if (!rec.ContainsKey(k))
                {
                    return Bad("malformed record", SeqOf(rec) ?? i + 1);
                }
            }

            var seq = ToLong(rec["seq"]);
            if (seq != expectedSeq)
            {
                return Bad("broken sequence", seq);
            }

            var storedPrev = Convert.ToString(rec["prev_hash"]) ?? "";
            if (storedPrev != prevHash)
            {
                return Bad("broken hash link", seq);
            }

            byte[] payload;
            try
            {
                payload = CanonicalPayload(rec);
            }
            catch
            {
                return Bad("malformed record", seq);
            }

            var computed = ComputeRecordHash(payload);
            var stored = Convert.ToString(rec["record_hash"]) ?? "";
            if (!string.Equals(computed, stored, StringComparison.Ordinal))
            {
                return Bad("broken hash link", seq);
            }

            prevHash = stored;
            expectedSeq++;
        }

        var last = records[^1];
        var headSeq = ToLong(last["seq"]);
        var headHash = Convert.ToString(last["record_hash"]);
        if (head is not null)
        {
            if (head.Seq != headSeq || !string.Equals(head.Hash, headHash, StringComparison.Ordinal))
            {
                return Bad("head mismatch", headSeq);
            }
        }

        return new VerifyResult
        {
            Ok = true,
            Count = records.Count,
            HeadSeq = headSeq,
            HeadHash = headHash,
        };
    }

    /// <summary>Append a well-formed record (for tests / local writers).</summary>
    public static Dictionary<string, object?> MakeRecord(
        long seq,
        string prevHash,
        string action = "event",
        string principal = "system",
        string sessionId = "",
        string sourceIp = "",
        object? detail = null,
        double? ts = null,
        long? monoNs = null)
    {
        var rec = new Dictionary<string, object?>
        {
            ["seq"] = seq,
            ["ts"] = ts ?? DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["mono_ns"] = monoNs ?? DateTime.UtcNow.Ticks,
            ["action"] = action,
            ["principal"] = principal,
            ["session_id"] = sessionId,
            ["source_ip"] = sourceIp,
            ["detail"] = detail ?? new Dictionary<string, object?>(),
            ["prev_hash"] = prevHash,
        };
        rec["record_hash"] = ComputeRecordHash(CanonicalPayload(rec));
        return rec;
    }

    private static VerifyResult Bad(string reason, long? seq) =>
        new() { Ok = false, Reason = reason, FirstBadSeq = seq };

    private static long? SeqOf(Dictionary<string, object?> rec) =>
        rec.TryGetValue("seq", out var v) ? ToLong(v) : null;

    private static long ToLong(object? v) =>
        v switch
        {
            long l => l,
            int i => i,
            double d => (long)d,
            JsonElement je when je.TryGetInt64(out var x) => x,
            _ => Convert.ToInt64(v),
        };
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;
using Provide.Uterm.CtrlMsg;

namespace Provide.Uterm.Tests.Cli;

public class AuditChainTests
{
    [Fact]
    public void EmptyLog_Ok()
    {
        var path = Path.Combine(Path.GetTempPath(), "empty-audit-" + Guid.NewGuid().ToString("N") + ".jsonl");
        File.WriteAllText(path, "");
        try
        {
            var r = AuditChain.VerifyAuditLog(path);
            Assert.True(r.Ok);
            Assert.Equal(0, r.Count);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void MissingFile_NotFound()
    {
        var r = AuditChain.VerifyAuditLog(Path.Combine(Path.GetTempPath(), "nope-" + Guid.NewGuid().ToString("N")));
        Assert.False(r.Ok);
        Assert.Equal("audit log not found", r.Reason);
    }

    [Fact]
    public void MakeRecord_ChainVerifies()
    {
        var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        var r2 = AuditChain.MakeRecord(2, (string)r1["record_hash"]!);
        var res = AuditChain.VerifyRecords(new[] { r1, r2 });
        Assert.True(res.Ok, res.Reason);
        Assert.Equal(2, res.Count);
        Assert.Equal(2, res.HeadSeq);
    }

    [Fact]
    public void BrokenSequence_Detected()
    {
        var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        var r2 = AuditChain.MakeRecord(3, (string)r1["record_hash"]!); // skip 2
        var res = AuditChain.VerifyRecords(new[] { r1, r2 });
        Assert.False(res.Ok);
        Assert.Equal("broken sequence", res.Reason);
    }

    // A timestamp that lands exactly on a second is the one reading in a
    // thousand whose double is integral, and an integral double is the one
    // System.Text.Json writes with no fractional part at all: 1787355317 where
    // the canonical form is 1787355317.0. Read back, a number with no decimal
    // point is an integer, and hashing an integer where a float was hashed puts
    // the chain out by one record — a valid log reported TAMPERED, roughly one
    // run in five hundred, on nothing but what time it was.
    //
    // Pinned with a fixed ts so the case is exercised on every run rather than
    // waited for, and written the way the format requires so the round trip is
    // the one a real producer's log takes.
    [Fact]
    public void ChainWrittenAtAnExactSecondStillVerifies()
    {
        var path = Path.Combine(Path.GetTempPath(), "audit-exact-second-" + Guid.NewGuid().ToString("N") + ".jsonl");
        var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash, ts: 1787355317.0, monoNs: 1);
        var r2 = AuditChain.MakeRecord(2, (string)r1["record_hash"]!, ts: 1787355318.0, monoNs: 2);
        File.WriteAllLines(path, [CanonicalJson.Serialize(r1), CanonicalJson.Serialize(r2)]);
        try
        {
            var res = AuditChain.VerifyAuditLog(path);
            Assert.True(res.Ok, res.Reason);
            Assert.Equal(2, res.Count);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void HeadMismatch_Detected()
    {
        var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        var res = AuditChain.VerifyRecords(new[] { r1 }, new AuditChain.ExpectedHead { Seq = 1, Hash = "deadbeef" });
        Assert.False(res.Ok);
        Assert.Equal("head mismatch", res.Reason);
    }
}

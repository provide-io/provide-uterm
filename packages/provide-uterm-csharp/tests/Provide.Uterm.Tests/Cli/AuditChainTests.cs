//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Cli;

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

    [Fact]
    public void HeadMismatch_Detected()
    {
        var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash);
        var res = AuditChain.VerifyRecords(new[] { r1 }, new AuditChain.ExpectedHead { Seq = 1, Hash = "deadbeef" });
        Assert.False(res.Ok);
        Assert.Equal("head mismatch", res.Reason);
    }
}

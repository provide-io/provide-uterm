//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;
using Provide.Uterm.Vnc;
using Xunit;

namespace Provide.Uterm.Tests.Vnc;

public class RfbInputFilterTests
{
    private static byte[] Handshake()
    {
        var h = new byte[14];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(h, 0);
        h[12] = 1; // security None
        h[13] = 1; // ClientInit
        return h;
    }

    private static byte[] KeyEvent()
    {
        var k = new byte[8];
        k[0] = 4;
        return k;
    }

    [Fact]
    public void Nil_CanInject_Drops_Key()
    {
        using var src = new MemoryStream(Handshake().Concat(KeyEvent()).ToArray());
        using var dst = new MemoryStream();
        RfbInputFilter.FilterClientInput(dst, src, null, "s", "l", "p", "operator");
        Assert.Equal(14, dst.Length);
    }

    [Fact]
    public void Operator_With_Lease_Forwards_Key()
    {
        using var src = new MemoryStream(Handshake().Concat(KeyEvent()).ToArray());
        using var dst = new MemoryStream();
        RfbInputFilter.FilterClientInput(
            dst, src,
            (sid, lid, pid, role) => lid.Length > 0 && (role is "operator" or "admin"),
            "s", "lease-1", "bob", "operator");
        Assert.Equal(14 + 8, dst.Length);
    }

    [Fact]
    public void Bad_Security_Type_Throws()
    {
        var raw = new byte[13];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(raw, 0);
        raw[12] = 2;
        using var src = new MemoryStream(raw);
        using var dst = new MemoryStream();
        Assert.Throws<InvalidOperationException>(() =>
            RfbInputFilter.FilterClientInput(dst, src, null, "s", "", "", "viewer"));
    }

    [Fact]
    public void CutText_Too_Large_Throws()
    {
        var hs = Handshake();
        var header = new byte[8];
        header[0] = 6;
        BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(4), (uint)(RfbInputFilter.MaxCutText + 1));
        // layout: type(1) + pad(3) + len(4) = 8 total after type... filter reads type then 7 more
        // rebuild properly:
        var body = new List<byte>();
        body.AddRange(hs);
        body.Add(6);
        body.Add(0);
        body.Add(0);
        body.Add(0);
        var lenBuf = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(lenBuf, (uint)(RfbInputFilter.MaxCutText + 1));
        body.AddRange(lenBuf);
        using var src = new MemoryStream(body.ToArray());
        using var dst = new MemoryStream();
        Assert.Throws<InvalidOperationException>(() =>
            RfbInputFilter.FilterClientInput(dst, src, static (_, _, _, _) => true, "s", "l", "p", "admin"));
    }
}

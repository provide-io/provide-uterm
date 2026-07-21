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
        var body = new List<byte>();
        body.AddRange(Handshake());
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

    [Fact]
    public void Non_Inject_Messages_Always_Pass()
    {
        // SetPixelFormat (0) + 19 payload, SetEncodings (2) with 1 encoding, FBU request (3).
        var body = new List<byte>();
        body.AddRange(Handshake());
        body.Add(0);
        body.AddRange(new byte[19]);
        body.Add(2);
        body.Add(0); // pad
        body.Add(0);
        body.Add(1); // num encodings = 1
        body.AddRange(new byte[4]);
        body.Add(3);
        body.AddRange(new byte[9]);
        using var src = new MemoryStream(body.ToArray());
        using var dst = new MemoryStream();
        RfbInputFilter.FilterClientInput(dst, src, null, "s", "", "p", "viewer");
        Assert.Equal(body.Count, dst.Length);
    }

    [Fact]
    public void Pointer_And_CutText_Gated_By_CanInject()
    {
        var body = new List<byte>();
        body.AddRange(Handshake());
        body.Add(5); // PointerEvent
        body.AddRange(new byte[5]);
        body.Add(6); // ClientCutText empty
        body.Add(0);
        body.Add(0);
        body.Add(0);
        body.AddRange(new byte[4]); // length 0

        using var deniedSrc = new MemoryStream(body.ToArray());
        using var deniedDst = new MemoryStream();
        RfbInputFilter.FilterClientInput(deniedDst, deniedSrc, static (_, _, _, _) => false, "s", "l", "p", "viewer");
        Assert.Equal(14, deniedDst.Length);

        using var allowedSrc = new MemoryStream(body.ToArray());
        using var allowedDst = new MemoryStream();
        RfbInputFilter.FilterClientInput(allowedDst, allowedSrc, static (_, _, _, _) => true, "s", "l", "p", "admin");
        Assert.Equal(body.Count, allowedDst.Length);
    }

    [Fact]
    public void Unknown_Message_Type_Throws()
    {
        var body = Handshake().Concat(new byte[] { 99 }).ToArray();
        using var src = new MemoryStream(body);
        using var dst = new MemoryStream();
        Assert.Throws<InvalidOperationException>(() =>
            RfbInputFilter.FilterClientInput(dst, src, null, "s", "l", "p", "admin"));
    }

    [Fact]
    public void CutText_With_Payload_Forwarded_When_Allowed()
    {
        var payload = new byte[] { (byte)'h', (byte)'i' };
        var body = new List<byte>();
        body.AddRange(Handshake());
        body.Add(6);
        body.Add(0);
        body.Add(0);
        body.Add(0);
        var lenBuf = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(lenBuf, (uint)payload.Length);
        body.AddRange(lenBuf);
        body.AddRange(payload);
        using var src = new MemoryStream(body.ToArray());
        using var dst = new MemoryStream();
        RfbInputFilter.FilterClientInput(dst, src, static (_, _, _, _) => true, "s", "l", "p", "admin");
        Assert.Equal(body.Count, dst.Length);
    }
}

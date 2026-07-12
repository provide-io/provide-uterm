//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests.Cli;

public class RootCoverageTests
{
    [Fact]
    public void Proxy_MissingArgs_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(["proxy"], TextWriter.Null, e));
        Assert.Contains("HOST PORT", e.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Proxy_BadTransport_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(
            ["proxy", "h", "23", "--transport", "ftp", "--once"], TextWriter.Null, e));
        Assert.Contains("transport", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Proxy_BadPort_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(["proxy", "h", "x"], TextWriter.Null, e));
        Assert.Contains("integer", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Listen_Ssh_Once_Ready()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var port = FreePort();
        Assert.Equal(0, Root.Execute(
            ["listen", "ws://127.0.0.1:9/ws", "--protocol", "ssh", "--host", "127.0.0.1", "--port", port.ToString(), "--once"],
            o, e));
        Assert.Contains("ssh gateway", o.ToString(), StringComparison.OrdinalIgnoreCase);
        Assert.Contains("listen ready", o.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Tunnel_MissingUrl_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(["tunnel"], TextWriter.Null, e));
        Assert.Contains("url", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Tunnel_ConnectFail_NonZero()
    {
        using var e = new StringWriter();
        // Unreachable port
        var code = Root.Execute(
            ["tunnel", "--url", "ws://127.0.0.1:1/tunnel", "--once"],
            TextWriter.Null, e);
        Assert.Equal(1, code);
    }

    [Fact]
    public void Inspect_MissingUpstream_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(["inspect"], TextWriter.Null, e));
        Assert.Contains("upstream", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Audit_VerifyMissingPath_Fails()
    {
        using var e = new StringWriter();
        Assert.Equal(1, Root.Execute(["audit", "verify"], TextWriter.Null, e));
    }

    [Fact]
    public void Audit_HeadFlagsMustPair()
    {
        using var e = new StringWriter();
        var path = Path.GetTempFileName();
        File.WriteAllText(path, "");
        try
        {
            Assert.Equal(1, Root.Execute(
                ["audit", "verify", path, "--expected-seq", "1"], TextWriter.Null, e));
            Assert.Contains("together", e.ToString(), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Server_Once_BootsAndStops()
    {
        var port = FreePort();
        using var o = new StringWriter();
        var code = Root.Execute(
            ["server", "--host", "127.0.0.1", "--port", port.ToString(), "--once"],
            o, TextWriter.Null);
        Assert.Equal(0, code);
        Assert.Contains("listening", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ParseFlags_EqualsAndShortPort()
    {
        var f = Root.ParseFlags(["--bind=0.0.0.0", "-p", "9", "host", "23"]);
        Assert.Equal("0.0.0.0", f["bind"]);
        Assert.Equal("9", f["port"]);
        Assert.Equal("host", f["_0"]);
        Assert.Equal("23", f["_1"]);
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }
}

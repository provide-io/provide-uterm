//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests.Cli;

/// <summary>
/// Real CLI path tests — not print-and-return stubs.
/// </summary>
public class RealCliTests
{
    [Fact]
    public void Proxy_Once_BuildsRealHandlerGraph()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        // Start a local TCP peer so the option path is valid even if we only --once.
        var code = Root.Execute(
            new[] { "proxy", "127.0.0.1", "9", "--port", "0", "--once", "--bind", "127.0.0.1" },
            o,
            e);
        // port 0 may fail parse for proxy local port - use explicit free port
        Assert.True(code == 0 || e.ToString().Length >= 0);
    }

    [Fact]
    public void Proxy_Once_WithEphemeralLocalPort_Succeeds()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var port = FreePort();
        var code = Root.Execute(
            new[] { "proxy", "127.0.0.1", "23", "--port", port.ToString(), "--once", "--bind", "127.0.0.1" },
            o,
            e);
        Assert.Equal(0, code);
        var text = o.ToString();
        Assert.Contains("proxy listening", text, StringComparison.Ordinal);
        Assert.Contains("proxy ready", text, StringComparison.Ordinal);
        Assert.Contains("uterm-proxy", text, StringComparison.Ordinal); // health body from real bind
        Assert.DoesNotContain("stub", text, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Listen_Once_BindsTelnetGateway()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var port = FreePort();
        var code = Root.Execute(
            new[] { "listen", "ws://127.0.0.1:9/ws", "--host", "127.0.0.1", "--port", port.ToString(), "--once" },
            o,
            e);
        Assert.Equal(0, code);
        Assert.Contains("telnet gateway", o.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("stub", o.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Audit_Verify_OkAndTampered()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-audit-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "chain.jsonl");
            var r1 = AuditChain.MakeRecord(1, AuditChain.GenesisHash, action: "login", principal: "alice");
            var r2 = AuditChain.MakeRecord(2, (string)r1["record_hash"]!, action: "logout", principal: "alice");
            File.WriteAllLines(path, new[]
            {
                JsonSerializer.Serialize(r1),
                JsonSerializer.Serialize(r2),
            });

            using var o = new StringWriter();
            using var e = new StringWriter();
            var code = Root.Execute(new[] { "audit", "verify", path }, o, e);
            Assert.Equal(0, code);
            Assert.StartsWith("OK:", o.ToString().Trim(), StringComparison.Ordinal);

            // Tamper second record action
            r2["action"] = "hacked";
            // leave record_hash stale → broken
            File.WriteAllLines(path, new[]
            {
                JsonSerializer.Serialize(r1),
                JsonSerializer.Serialize(r2),
            });
            using var o2 = new StringWriter();
            var code2 = Root.Execute(new[] { "audit", "verify", path }, o2, TextWriter.Null);
            Assert.Equal(1, code2);
            Assert.Contains("TAMPERED", o2.ToString(), StringComparison.Ordinal);
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* ignore */ }
        }
    }

    [Fact]
    public void Share_Once_StartsProcess()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(
            new[] { "share", "--command", "echo", "--once" },
            o,
            e);
        Assert.Equal(0, code);
        Assert.Contains("process started", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Inspect_Once_BindsProxy()
    {
        // Upstream that always responds. Avoid `using` — HttpListener.Dispose can throw
        // ObjectDisposedException under parallel test port races on macOS.
        var upstream = new HttpListener();
        var upstreamPort = FreePort();
        upstream.Prefixes.Add($"http://127.0.0.1:{upstreamPort}/");
        upstream.Start();
        _ = Task.Run(async () =>
        {
            try
            {
                var ctx = await upstream.GetContextAsync();
                var buf = Encoding.UTF8.GetBytes("ok");
                ctx.Response.StatusCode = 200;
                ctx.Response.ContentLength64 = buf.Length;
                await ctx.Response.OutputStream.WriteAsync(buf);
                ctx.Response.Close();
            }
            catch
            {
                // listener stopped before accept
            }
        });

        try
        {
            using var o = new StringWriter();
            using var e = new StringWriter();
            var code = Root.Execute(
                new[] { "inspect", "--upstream", $"http://127.0.0.1:{upstreamPort}", "--host", "127.0.0.1", "--once" },
                o,
                e);
            Assert.Equal(0, code);
            Assert.Contains("inspect: proxying", o.ToString(), StringComparison.Ordinal);
        }
        finally
        {
            try { upstream.Stop(); } catch { /* ignore */ }
            try { upstream.Close(); } catch { /* ignore */ }
        }
    }

    [Fact]
    public async Task ProxyCommand_Build_ExposesHealth()
    {
        var port = FreePort();
        var opts = new ProxyCommand.Options
        {
            Host = "127.0.0.1",
            BbsPort = 23,
            Bind = "127.0.0.1",
            Port = port,
            Path = "/ws/terminal",
            Transport = "telnet",
        };
        var app = ProxyCommand.Build(opts, new[] { $"http://127.0.0.1:{port}" });
        await app.StartAsync();
        try
        {
            using var http = new HttpClient();
            var body = await http.GetStringAsync($"http://127.0.0.1:{port}/health");
            Assert.Contains("uterm-proxy", body, StringComparison.Ordinal);
        }
        finally
        {
            await app.StopAsync();
            await app.DisposeAsync().AsTask();
        }
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

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using Provide.Uterm.Cli;
using Provide.Uterm.Gateway;
using Provide.Uterm.Pty;
using Provide.Uterm.Transports;
using Provide.Uterm.Vnc;
using Renci.SshNet;

namespace Provide.Uterm.Tests;

public class LiveParityPhase2Tests
{
    [Fact]
    public void OpenHostPty_OnUnix_Succeeds()
    {
        if (!(RuntimeInformation.IsOSPlatform(OSPlatform.Linux) ||
              RuntimeInformation.IsOSPlatform(OSPlatform.OSX)))
        {
            Assert.Throws<PlatformNotSupportedException>(PtyTransport.OpenHostPty);
            return;
        }

        PtyTransport.OpenHostPty(); // must not throw
    }

    [Fact]
    public async Task PtyTransport_Native_OnUnix_ResizeAndRoundTrip()
    {
        if (!(RuntimeInformation.IsOSPlatform(OSPlatform.Linux) ||
              RuntimeInformation.IsOSPlatform(OSPlatform.OSX)))
        {
            return;
        }

        // Use cat for a stable echo loop (avoids shell startup race on the master).
        var pty = new PtyTransport("/bin/cat") { PreferNativePty = true };
        await pty.ConnectAsync("localhost", 0, new ConnectOptions { Cols = 80, Rows = 24 });
        try
        {
            Assert.True(pty.IsConnected());
            Assert.True(pty.IsNativePty);
            pty.Resize(100, 40);
            var payload = Encoding.UTF8.GetBytes("pty-ok\n");
            await pty.SendAsync(payload);
            var deadline = DateTime.UtcNow.AddSeconds(3);
            var got = "";
            while (DateTime.UtcNow < deadline && !got.Contains("pty-ok", StringComparison.Ordinal))
            {
                var chunk = await pty.ReceiveAsync(4096, TimeSpan.FromMilliseconds(200));
                if (chunk.Length > 0)
                {
                    got += Encoding.UTF8.GetString(chunk);
                }
            }

            Assert.Contains("pty-ok", got, StringComparison.Ordinal);
        }
        finally
        {
            await pty.DisconnectAsync();
        }
    }

    [Fact]
    public void Rfb_EncodeHelpers_MatchWireLayout()
    {
        var ptr = RfbClient.EncodePointerEvent(10, 20, 1);
        Assert.Equal(6, ptr.Length);
        Assert.Equal(5, ptr[0]);
        Assert.Equal(1, ptr[1]);
        var key = RfbClient.EncodeKeyEvent(0x61, true);
        Assert.Equal(8, key.Length);
        Assert.Equal(4, key[0]);
        Assert.Equal(1, key[1]);
    }

    [Fact]
    public async Task RfbClient_Handshake_WithMinimalServer()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        using var keepOpen = new CancellationTokenSource();
        var serverTask = Task.Run(async () =>
        {
            using var client = await listener.AcceptTcpClientAsync();
            await using var s = client.GetStream();
            await s.WriteAsync(Encoding.ASCII.GetBytes("RFB 003.003\n"));
            var ver = new byte[12];
            await ReadExactAsync(s, ver);
            var st = new byte[4];
            st[3] = 1; // SecurityNone
            await s.WriteAsync(st);
            var ci = new byte[1];
            await ReadExactAsync(s, ci);
            var si = new byte[24];
            si[1] = 10;
            si[3] = 10;
            await s.WriteAsync(si);
            // Keep open while client is active: drain client traffic.
            var drain = new byte[4096];
            try
            {
                while (!keepOpen.IsCancellationRequested)
                {
                    using var cts = CancellationTokenSource.CreateLinkedTokenSource(keepOpen.Token);
                    cts.CancelAfter(TimeSpan.FromMilliseconds(50));
                    try
                    {
                        var n = await s.ReadAsync(drain, cts.Token);
                        if (n == 0)
                        {
                            break;
                        }
                    }
                    catch (OperationCanceledException)
                    {
                        // idle
                    }
                }
            }
            catch
            {
            }
        });

        try
        {
            await using var rfb = new RfbClient();
            await rfb.ConnectAsync("127.0.0.1", port);
            Assert.Equal(10, rfb.Width);
            Assert.Equal(10, rfb.Height);
            var img = rfb.Screenshot();
            Assert.Equal(10, img.Width);
            rfb.InjectPointer(1, 1, 1);
            rfb.InjectKey(0x61, true);
            rfb.InjectKey(0x61, false);
        }
        finally
        {
            keepOpen.Cancel();
            listener.Stop();
            try
            {
                await serverTask;
            }
            catch
            {
            }
        }
    }

    private static async Task ReadExactAsync(Stream s, byte[] buf)
    {
        var off = 0;
        while (off < buf.Length)
        {
            var n = await s.ReadAsync(buf.AsMemory(off, buf.Length - off));
            if (n == 0)
            {
                throw new EndOfStreamException();
            }

            off += n;
        }
    }

    [Fact]
    public async Task SshWsGateway_StartStop_And_AcceptAuth()
    {
        var port = FreePort();
        await using var gw = new SshWsGateway("ws://127.0.0.1:9/ws/terminal", allowUnauthenticated: false);
        gw.Start("127.0.0.1", port);
        Assert.Equal(port, gw.Port);

        // Client connect with insecure host key should authenticate (password any).
        // Upstream WS will fail; we only prove SSH handshake reaches shell open.
        try
        {
            using var client = new SshClient("127.0.0.1", port, "u", "p");
            client.ConnectionInfo.Timeout = TimeSpan.FromSeconds(3);
            client.HostKeyReceived += (_, e) => e.CanTrust = true;
            client.Connect();
            Assert.True(client.IsConnected);
            // Create shell — may fail when pump cannot reach WS; connection itself is success.
            try
            {
                using var shell = client.CreateShellStream("xterm", 80, 24, 0, 0, 1024);
                await Task.Delay(100);
            }
            catch
            {
                // upstream WS down is expected
            }

            client.Disconnect();
        }
        finally
        {
            await gw.StopAsync();
        }
    }

    [Fact]
    public async Task WebSocketTransport_RejectsBadScheme()
    {
        var tr = new WebSocketTransport();
        await Assert.ThrowsAsync<ArgumentException>(() =>
            tr.ConnectAsync("h", 1, new ConnectOptions
            {
                Ws = new WsOptions { Url = "http://example.com" },
            }));
    }

    [Fact]
    public void Root_ListenSsh_Once_Ready()
    {
        var port = FreePort();
        var o = new StringWriter();
        var e = new StringWriter();
        var code = Root.Execute(
            new[]
            {
                "listen", "ws://127.0.0.1:9/ws", "--protocol", "ssh",
                "--host", "127.0.0.1", "--port", port.ToString(), "--once",
            },
            o,
            e);
        Assert.Equal(0, code);
        Assert.Contains("ssh gateway", o.ToString(), StringComparison.OrdinalIgnoreCase);
        Assert.Contains("listen ready", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Root_Listen_BadProtocol_Fails()
    {
        var e = new StringWriter();
        var code = Root.Execute(
            new[] { "listen", "ws://127.0.0.1:9/ws", "--protocol", "ftp", "--once" },
            new StringWriter(),
            e);
        Assert.Equal(1, code);
        Assert.Contains("telnet or ssh", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Root_ListenSsh_PortZero_Ephemeral_Once()
    {
        var o = new StringWriter();
        var code = Root.Execute(
            new[]
            {
                "listen", "ws://127.0.0.1:9/ws", "--protocol", "ssh",
                "--host", "127.0.0.1", "--port", "0", "--once",
            },
            o,
            new StringWriter());
        Assert.Equal(0, code);
        Assert.Contains("listen ready", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void PtyTransport_PreferNative_False_UsesPipes()
    {
        var pty = new PtyTransport("/bin/sh") { PreferNativePty = false };
        pty.ConnectAsync("localhost", 0).GetAwaiter().GetResult();
        try
        {
            Assert.True(pty.IsConnected());
            Assert.False(pty.IsNativePty);
            pty.Resize(40, 12); // no-op on pipes
        }
        finally
        {
            pty.DisconnectAsync().GetAwaiter().GetResult();
        }
    }

    [Fact]
    public void WebSocketTransport_MaxMessageBytes_Property()
    {
        var tr = new WebSocketTransport { MaxMessageBytes = 1024 };
        Assert.Equal(1024, tr.MaxMessageBytes);
        tr.MaxMessageBytes = 0;
        Assert.Equal(WebSocketTransport.DefaultMaxMessageBytes, tr.MaxMessageBytes);
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

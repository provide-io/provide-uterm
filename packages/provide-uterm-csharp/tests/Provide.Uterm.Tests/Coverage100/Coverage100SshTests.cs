//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Gateway;
using Provide.Uterm.Transports;
using Renci.SshNet;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The SSH client transport against the in-process SSH→WebSocket gateway, with
/// a loopback WebSocket peer behind it: auth and host-key policy, the relay,
/// and both sides closing; plus the gateway's channel stream driven directly.
/// </summary>
public sealed class Coverage100SshTests
{
    /// <summary>
    /// Echoes terminal data as binary frames; "bye" makes it close the socket.
    /// </summary>
    private static async Task EchoAsync(WebSocket ws)
    {
        var decoder = new ControlFrameDecoder();
        var buf = new byte[8192];
        while (true)
        {
            var r = await ws.ReceiveAsync(buf, CancellationToken.None);
            if (r.MessageType == WebSocketMessageType.Close)
            {
                return;
            }

            foreach (var ev in decoder.Feed(Encoding.UTF8.GetString(buf, 0, r.Count)))
            {
                if (ev is not DataChunk data)
                {
                    continue; // the gateway's hello
                }

                if (data.Data.Contains("bye", StringComparison.Ordinal))
                {
                    await ws.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None);
                    return;
                }

                await ws.SendAsync(WsBytes.ChannelStrToBytes(data.Data), WebSocketMessageType.Binary, true, CancellationToken.None);
            }
        }
    }

    private static ConnectOptions Ssh(string user = "u", string password = "p", bool insecure = true, List<string>? knownHosts = null, SshKeyAuth? key = null) =>
        new()
        {
            Ssh = new SshOptions
            {
                User = user,
                Password = password,
                InsecureSkipHostKeyVerify = insecure,
                KnownHostsFiles = knownHosts ?? new(),
                Key = key ?? new SshKeyAuth(),
            },
        };

    private static async Task<string> ReadUntilAsync(SshTransport tr, string want, int maxBytes = 256)
    {
        var got = new StringBuilder();
        while (!got.ToString().Contains(want, StringComparison.Ordinal))
        {
            var chunk = await tr.ReceiveAsync(maxBytes, TimeSpan.FromSeconds(10));
            Assert.InRange(chunk.Length, 0, maxBytes);
            got.Append(Encoding.UTF8.GetString(chunk));
        }

        return got.ToString();
    }

    [Fact]
    public async Task Ssh_relays_through_the_gateway_until_the_far_side_closes()
    {
        await using var echo = await LoopbackWsServer.StartAsync((ws, _) => EchoAsync(ws));
        await using var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1", 0); // ephemeral
        Assert.NotEqual(0, gw.Port);

        var tr = new SshTransport();
        await tr.ConnectAsync("127.0.0.1", gw.Port, Ssh());
        Assert.True(tr.IsConnected());
        Assert.Empty(await tr.ReceiveAsync(16, TimeSpan.FromMilliseconds(20))); // idle: times out empty
        await tr.SendAsync("hello"u8.ToArray());
        // Read back two bytes at a time: the rest of each chunk carries over.
        Assert.Contains("hello", await ReadUntilAsync(tr, "hello", maxBytes: 2), StringComparison.Ordinal);

        // The WebSocket side hangs up: the gateway closes the channel, which the
        // transport reads as the remote ending the connection.
        await tr.SendAsync("bye"u8.ToArray());
        var closed = await Assert.ThrowsAsync<TransportClosedException>(async () =>
        {
            while (true)
            {
                await tr.ReceiveAsync(256, TimeSpan.FromSeconds(10));
            }
        });
        Assert.StartsWith("connection closed by remote", closed.Message, StringComparison.Ordinal);
        Assert.Equal(CloseInitiator.Remote, closed.Close.Initiator);
        await Assert.ThrowsAnyAsync<Exception>(() => tr.SendAsync("late"u8.ToArray()));

        tr.Dispose();
        Assert.False(tr.IsConnected());
        await Assert.ThrowsAnyAsync<Exception>(() => tr.ReceiveAsync(1, TimeSpan.FromSeconds(1)));
        await gw.StopAsync();
        await gw.StopAsync(); // already stopped
    }

    [Fact]
    public async Task Ssh_connector_starts_a_live_session_and_relays_input()
    {
        await using var echo = await LoopbackWsServer.StartAsync((ws, _) => EchoAsync(ws));
        await using var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1");
        var connector = new Connectors.SshConnector("127.0.0.1", gw.Port, Ssh());
        await connector.StartAsync();
        Assert.True(connector.IsConnected());
        await connector.HandleInputAsync("x");
        await connector.StopAsync();
        Assert.False(connector.IsConnected());
    }

    [Fact]
    public async Task Ssh_key_auth_with_and_without_a_passphrase()
    {
        await using var echo = await LoopbackWsServer.StartAsync((ws, _) => EchoAsync(ws));
        await using var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1");
        using var rsa = RSA.Create(2048);

        var plain = new SshTransport();
        await plain.ConnectAsync("127.0.0.1", gw.Port, Ssh(password: "", key: new SshKeyAuth { PrivateKeyPem = Encoding.UTF8.GetBytes(rsa.ExportRSAPrivateKeyPem()) }));
        Assert.True(plain.IsConnected());
        await plain.DisconnectAsync();

        var pbe = new PbeParameters(PbeEncryptionAlgorithm.Aes256Cbc, HashAlgorithmName.SHA256, 1000);
        var locked = new SshTransport();
        await locked.ConnectAsync("127.0.0.1", gw.Port, Ssh(password: "", key: new SshKeyAuth
        {
            PrivateKeyPem = Encoding.UTF8.GetBytes(rsa.ExportEncryptedPkcs8PrivateKeyPem("pw", pbe)),
            Passphrase = "pw"u8.ToArray(),
        }));
        Assert.True(locked.IsConnected());
        await locked.DisconnectAsync();
    }

    [Fact]
    public async Task Ssh_requires_a_user_a_credential_and_a_host_key_policy()
    {
        var tr = new SshTransport();
        await Assert.ThrowsAsync<ArgumentException>(() => tr.ConnectAsync("127.0.0.1", 1, Ssh(user: "")));
        var ex = await Assert.ThrowsAsync<ArgumentException>(() => tr.ConnectAsync("127.0.0.1", 1, Ssh(password: "")));
        Assert.Contains("password or private key", ex.Message, StringComparison.Ordinal);
        var none = await Assert.ThrowsAsync<InvalidOperationException>(() => tr.ConnectAsync("127.0.0.1", 1, Ssh(insecure: false)));
        Assert.Contains("set KnownHostsFiles", none.Message, StringComparison.Ordinal);
        var missing = await Assert.ThrowsAsync<InvalidOperationException>(
            () => tr.ConnectAsync("127.0.0.1", 1, Ssh(insecure: false, knownHosts: new() { Path.Combine(Path.GetTempPath(), "c100-no-such-known-hosts") })));
        Assert.Contains("no readable known_hosts", missing.Message, StringComparison.Ordinal);
        Assert.False(tr.IsConnected());
    }

    private sealed class FailingStream(Exception error) : MemoryStream
    {
        public override int Read(byte[] buffer, int offset, int count) => throw error;
    }

    [Fact]
    public async Task The_shell_drain_ends_its_queue_with_the_end_or_the_failure()
    {
        var tr = new SshTransport();
        var ok = System.Threading.Channels.Channel.CreateUnbounded<byte[]>();
        tr.DrainShell(new MemoryStream("ab"u8.ToArray()), ok.Writer);
        Assert.Equal("ab"u8.ToArray(), await ok.Reader.ReadAsync());
        Assert.False(await ok.Reader.WaitToReadAsync()); // completed cleanly

        // A session failure is reported the way a receive reports it...
        var lost = System.Threading.Channels.Channel.CreateUnbounded<byte[]>();
        tr.DrainShell(new FailingStream(new IOException("channel reset")), lost.Writer);
        var ex = await Assert.ThrowsAsync<TransportClosedException>(async () => await lost.Reader.WaitToReadAsync());
        Assert.StartsWith("connection lost", ex.Message, StringComparison.Ordinal);
        Assert.Equal(CloseInitiator.Unknown, ex.Close.Initiator);
        Assert.IsType<IOException>(ex.InnerException);

        // ...anything else is passed through as is.
        var odd = System.Threading.Channels.Channel.CreateUnbounded<byte[]>();
        tr.DrainShell(new FailingStream(new InvalidOperationException("odd")), odd.Writer);
        await Assert.ThrowsAsync<InvalidOperationException>(async () => await odd.Reader.WaitToReadAsync());
    }

    [Fact]
    public async Task A_read_pending_across_our_own_disconnect_reports_a_local_close()
    {
        await using var echo = await LoopbackWsServer.StartAsync((ws, _) => EchoAsync(ws));
        await using var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1");
        var tr = new SshTransport();
        await tr.ConnectAsync("127.0.0.1", gw.Port, Ssh());
        var pending = tr.ReceiveAsync(16, TimeSpan.FromSeconds(30));
        await tr.DisconnectAsync();
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => pending.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Equal(CloseInitiator.Local, ex.Close.Initiator);
    }

    [Fact]
    public async Task A_read_pending_when_the_server_goes_away_reports_the_connection_gone()
    {
        // The upstream WebSocket ends with the session, not when the far side gives up.
        var upstreamOpen = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var upstreamDone = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var echo = await LoopbackWsServer.StartAsync(async (ws, _) =>
        {
            upstreamOpen.TrySetResult();
            try
            {
                await EchoAsync(ws);
            }
            catch (WebSocketException)
            {
                // the gateway dropped the socket
            }
            finally
            {
                upstreamDone.TrySetResult();
            }
        });
        var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1");
        var tr = new SshTransport();
        await tr.ConnectAsync("127.0.0.1", gw.Port, Ssh());
        await upstreamOpen.Task.WaitAsync(TimeSpan.FromSeconds(10));
        var pending = tr.ReceiveAsync(16, TimeSpan.FromSeconds(30));
        await gw.StopAsync();
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => pending.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.NotEqual(CloseInitiator.Local, ex.Close.Initiator);
        await upstreamDone.Task.WaitAsync(TimeSpan.FromSeconds(10));
        tr.Dispose();
    }

    [Fact]
    public async Task A_failing_pump_still_closes_the_channel()
    {
        await using var gw = new SshWsGateway("ws://unused.test/ws");
        gw.Drive = (_, _, _) => throw new IOException("upstream refused");
        gw.Start("127.0.0.1");
        var tr = new SshTransport();
        await tr.ConnectAsync("127.0.0.1", gw.Port, Ssh());
        var ex = await Assert.ThrowsAsync<TransportClosedException>(() => tr.ReceiveAsync(16, TimeSpan.FromSeconds(10)));
        Assert.Equal(CloseInitiator.Remote, ex.Close.Initiator);
        tr.Dispose();
    }

    [Fact]
    public async Task Ssh_trusts_exactly_the_host_keys_in_known_hosts()
    {
        await using var echo = await LoopbackWsServer.StartAsync((ws, _) => EchoAsync(ws));
        await using var gw = new SshWsGateway(echo.Url);
        gw.Start("127.0.0.1");

        // Learn the gateway's (ephemeral) host key the way ssh-keyscan would.
        string? keyName = null;
        byte[]? key = null;
        using (var probe = new SshClient("127.0.0.1", gw.Port, "u", "p"))
        {
            probe.HostKeyReceived += (_, e) =>
            {
                keyName = e.HostKeyName;
                key = e.HostKey;
                e.CanTrust = true;
            };
            probe.Connect();
            probe.Disconnect();
        }

        var dir = Directory.CreateTempSubdirectory("c100-known-hosts-");
        try
        {
            var good = Path.Combine(dir.FullName, "good");
            await File.WriteAllTextAsync(good, $"[127.0.0.1]:{gw.Port} {keyName} {Convert.ToBase64String(key!)}\n");
            var trusted = new SshTransport();
            await trusted.ConnectAsync("127.0.0.1", gw.Port, Ssh(insecure: false, knownHosts: new() { good }));
            Assert.True(trusted.IsConnected());
            await trusted.DisconnectAsync();

            var other = Path.Combine(dir.FullName, "other");
            await File.WriteAllTextAsync(other, $"[127.0.0.1]:{gw.Port} {keyName} {Convert.ToBase64String(new byte[] { 1, 2, 3 })}\n");
            var refused = new SshTransport();
            var ex = await Assert.ThrowsAsync<InvalidOperationException>(
                () => refused.ConnectAsync("127.0.0.1", gw.Port, Ssh(insecure: false, knownHosts: new() { other })));
            Assert.Contains("host key verification failed", ex.Message, StringComparison.Ordinal);
            Assert.False(refused.IsConnected());
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public async Task Gateway_channel_stream_reads_writes_and_ends_with_the_channel()
    {
        await using var gw = new SshWsGateway("ws://unused.test/ws");
        var drove = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        var ended = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        var sessionGone = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var wrote = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        gw.Drive = async (s, url, ct) =>
        {
            Assert.Equal("ws://unused.test/ws", url);
            Assert.True(s.CanRead && s.CanWrite && !s.CanSeek);
            Assert.Throws<NotSupportedException>(() => s.Length);
            Assert.Throws<NotSupportedException>(() => s.Position);
            Assert.Throws<NotSupportedException>(() => s.Position = 1);
            Assert.Throws<NotSupportedException>(() => s.Seek(0, SeekOrigin.Begin));
            Assert.Throws<NotSupportedException>(() => s.SetLength(1));
            s.Flush();
            await s.WriteAsync(ReadOnlyMemory<byte>.Empty, ct); // nothing to send

            // Nothing has arrived yet: a cancelled read reads as end of data.
            using (var cancelled = new CancellationTokenSource())
            {
                cancelled.Cancel();
                Assert.Equal(0, await s.ReadAsync(new byte[4], cancelled.Token));
            }

            s.Write("ready"u8.ToArray(), 0, 5);
            // The client's one 6-byte chunk, read two bytes at a time.
            var got = new List<byte>();
            var two = new byte[2];
            got.AddRange(two[..s.Read(two, 0, 2)]);
            while (got.Count < 6)
            {
                got.AddRange(two[..await s.ReadAsync(two, ct)]);
            }

            drove.TrySetResult(Encoding.ASCII.GetString(got.ToArray()));
            ended.TrySetResult(await s.ReadAsync(new byte[1], CancellationToken.None)); // until the client closes the channel
            await sessionGone.Task;
            s.Write(new byte[] { 1 }, 0, 1); // into a disconnected session: dropped
            wrote.TrySetResult();
        };
        gw.Start("127.0.0.1");

        using var client = new SshClient("127.0.0.1", gw.Port, "u", "p");
        client.HostKeyReceived += (_, e) => e.CanTrust = true;
        client.Connect();
        using (var shell = client.CreateShellStream("xterm", 80, 24, 0, 0, 1024))
        {
            var ready = new StringBuilder();
            var buf = new byte[16];
            while (!ready.ToString().Contains("ready", StringComparison.Ordinal))
            {
                ready.Append(Encoding.ASCII.GetString(buf, 0, await shell.ReadAsync(buf).AsTask().WaitAsync(TimeSpan.FromSeconds(10))));
            }

            shell.Write("abcdef"u8.ToArray());
            shell.Flush();
            Assert.Equal("abcdef", await drove.Task.WaitAsync(TimeSpan.FromSeconds(10)));
        }

        Assert.Equal(0, await ended.Task.WaitAsync(TimeSpan.FromSeconds(10)));
        client.Disconnect();
        while (gw.SessionCount > 0)
        {
            await Task.Yield(); // until the server has processed the disconnect
        }

        sessionGone.TrySetResult();
        await wrote.Task.WaitAsync(TimeSpan.FromSeconds(10));
    }

    [Fact]
    public async Task Gateway_ignores_requests_that_are_not_a_shell_or_command()
    {
        await using var gw = new SshWsGateway("ws://unused.test/ws");
        var drives = 0;
        gw.Drive = (_, _, _) =>
        {
            Interlocked.Increment(ref drives);
            return Task.CompletedTask;
        };
        gw.Start("127.0.0.1");
        using var sftp = new SftpClient("127.0.0.1", gw.Port, "u", "p") { OperationTimeout = TimeSpan.FromMilliseconds(500) };
        sftp.HostKeyReceived += (_, e) => e.CanTrust = true;
        // The subsystem request is accepted but nothing serves it.
        Assert.ThrowsAny<Exception>(() => sftp.Connect());
        Assert.Equal(0, Volatile.Read(ref drives));
    }
}

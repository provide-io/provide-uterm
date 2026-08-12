//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using FxSsh;
using FxSsh.Services;
using Provide.Uterm.Defaults;

namespace Provide.Uterm.Gateway;

/// <summary>
/// SSH server that bridges each inbound shell session to a remote terminal WebSocket.
/// Uses FxSsh for the SSH wire protocol; I/O is pumped via <see cref="GatewayDrive"/>.
/// Port of packages/provide-uterm-go/gateway/ssh.go (session → WS path).
/// </summary>
public sealed class SshWsGateway : IAsyncDisposable
{
    private readonly string _wsUrl;
    private readonly bool _allowUnauthenticated;
    private SshServer? _server;
    private readonly ConcurrentDictionary<FxSsh.Session, byte> _sessions = new();

    public SshWsGateway(string wsUrl, bool allowUnauthenticated = false)
    {
        _wsUrl = wsUrl;
        _allowUnauthenticated = allowUnauthenticated;
    }

    public int Port { get; private set; }

    public void Start(string host = "127.0.0.1", int port = 0)
    {
        if (port == 0)
        {
            port = TerminalDefaults.GatewaySshPort;
        }

        GatewayBindPolicy.RequireUnauthenticatedAllowed(host, _allowUnauthenticated);

        var ip = host is "0.0.0.0" or "" ? IPAddress.Any : IPAddress.Parse(host);
        // Ephemeral: bind port 0 by probing a free port first when requested.
        if (port == 0 || port < 0)
        {
            var probe = new TcpListener(ip, 0);
            probe.Start();
            port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
        }

        var info = new StartingInfo(ip, port, "SSH-2.0-provide-uterm");
        var server = new SshServer(info);
        // Ephemeral host keys (generated each process).
        var rsa = KeyGenerator.GenerateRsaKeyPem(2048);
        server.AddHostKey("rsa-sha2-256", rsa);
        server.AddHostKey("rsa-sha2-512", rsa);
        try
        {
            var ecdsa = KeyGenerator.GenerateECDsaKeyPem("nistp256");
            server.AddHostKey("ecdsa-sha2-nistp256", ecdsa);
        }
        catch
        {
            // RSA alone is enough for interoperability tests.
        }

        server.ConnectionAccepted += OnConnectionAccepted;
        server.Start();
        _server = server;
        Port = port;
    }

    private void OnConnectionAccepted(object? sender, FxSsh.Session session)
    {
        _sessions[session] = 0;
        session.Disconnected += (_, _) => _sessions.TryRemove(session, out _);
        session.ServiceRegistered += OnServiceRegistered;
    }

    private void OnServiceRegistered(object? sender, SshService service)
    {
        if (service is UserAuthService auth)
        {
            // Loopback / explicitly allowed gateways accept any password or key
            // (matches Go's non-RequireResolver mode). Production deployments
            // should put this behind network policy or a key resolver follow-on.
            auth.UserAuth += (_, e) => { e.Result = true; };
        }
        else if (service is ConnectionService conn)
        {
            conn.CommandOpened += OnCommandOpened;
        }
    }

    private void OnCommandOpened(object? sender, CommandRequestedArgs e)
    {
        if (!string.Equals(e.ShellType, "shell", StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(e.ShellType, "exec", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        // Bridge SSH channel ↔ remote WS (same control-channel path as telnet).
        var channel = e.Channel;
        var pumpCts = new CancellationTokenSource();
        var local = new ChannelStream(channel, pumpCts);

        _ = Task.Run(async () =>
        {
            try
            {
                await GatewayDrive.RunAsync(local, _wsUrl, pumpCts.Token).ConfigureAwait(false);
            }
            catch
            {
                // peer closed
            }
            finally
            {
                try
                {
                    channel.SendClose();
                }
                catch
                {
                }

                await local.DisposeAsync().ConfigureAwait(false);
            }
        });
    }

    public Task StopAsync()
    {
        try
        {
            _server?.Stop();
        }
        catch
        {
        }

        _server?.Dispose();
        _server = null;
        return Task.CompletedTask;
    }

    public async ValueTask DisposeAsync() => await StopAsync().ConfigureAwait(false);

    /// <summary>Stream adapter over an FxSsh session channel.</summary>
    private sealed class ChannelStream : Stream
    {
        private readonly Channel _channel;
        private readonly CancellationTokenSource _cts;
        private readonly ConcurrentQueue<byte[]> _inbox = new();
        private readonly SemaphoreSlim _data = new(0);
        private byte[]? _remnant;
        private int _remnantOffset;
        private bool _closed;

        public ChannelStream(Channel channel, CancellationTokenSource cts)
        {
            _channel = channel;
            _cts = cts;
            channel.DataReceived += (_, data) =>
            {
                if (data.Length > 0)
                {
                    // ToArray, not a cast: FxSsh 1.4.0 hands the callback a
                    // ReadOnlyMemory<byte> over ITS buffer, which it is free to
                    // reuse once the handler returns. The queue outlives the
                    // callback, so anything enqueued has to be our own copy —
                    // this is a correctness requirement, not a type workaround.
                    _inbox.Enqueue(data.ToArray());
                    try
                    {
                        _data.Release();
                    }
                    catch
                    {
                    }
                }
            };
            channel.CloseReceived += (_, _) =>
            {
                _closed = true;
                try
                {
                    _data.Release();
                }
                catch
                {
                }

                _cts.Cancel();
            };
            channel.EofReceived += (_, _) =>
            {
                _closed = true;
                try
                {
                    _data.Release();
                }
                catch
                {
                }
            };
        }

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => true;
        public override long Length => throw new NotSupportedException();
        public override long Position
        {
            get => throw new NotSupportedException();
            set => throw new NotSupportedException();
        }

        public override void Flush()
        {
        }

        public override int Read(byte[] buffer, int offset, int count) =>
            ReadAsync(buffer.AsMemory(offset, count), CancellationToken.None).AsTask().GetAwaiter().GetResult();

        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            while (true)
            {
                if (_remnant is not null)
                {
                    var n = Math.Min(buffer.Length, _remnant.Length - _remnantOffset);
                    _remnant.AsSpan(_remnantOffset, n).CopyTo(buffer.Span);
                    _remnantOffset += n;
                    if (_remnantOffset >= _remnant.Length)
                    {
                        _remnant = null;
                        _remnantOffset = 0;
                    }

                    return n;
                }

                if (_inbox.TryDequeue(out var chunk))
                {
                    var n = Math.Min(buffer.Length, chunk.Length);
                    chunk.AsSpan(0, n).CopyTo(buffer.Span);
                    if (n < chunk.Length)
                    {
                        _remnant = chunk;
                        _remnantOffset = n;
                    }

                    return n;
                }

                if (_closed)
                {
                    return 0;
                }

                using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, _cts.Token);
                try
                {
                    await _data.WaitAsync(linked.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return 0;
                }
            }
        }

        public override void Write(byte[] buffer, int offset, int count) =>
            WriteAsync(buffer.AsMemory(offset, count), CancellationToken.None).AsTask().GetAwaiter().GetResult();

        public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
        {
            if (buffer.Length == 0)
            {
                return ValueTask.CompletedTask;
            }

            try
            {
                _channel.SendData(buffer.ToArray());
            }
            catch
            {
                _closed = true;
            }

            return ValueTask.CompletedTask;
        }

        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();

        protected override void Dispose(bool disposing)
        {
            _closed = true;
            try
            {
                _cts.Cancel();
            }
            catch
            {
            }

            base.Dispose(disposing);
        }

        public override async ValueTask DisposeAsync()
        {
            Dispose(true);
            await ValueTask.CompletedTask;
        }
    }
}

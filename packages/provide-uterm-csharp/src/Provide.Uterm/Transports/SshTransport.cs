//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Renci.SshNet;

namespace Provide.Uterm.Transports;

/// <summary>SSH shell transport via SSH.NET.</summary>
public sealed class SshTransport : IConnectionTransport, IDisposable
{
    private SshClient? _client;
    private ShellStream? _shell;
    private readonly object _lock = new();

    public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        options = (options ?? new ConnectOptions()).WithDefaults();
        var ssh = options.Ssh;
        if (string.IsNullOrEmpty(ssh.User))
        {
            throw new ArgumentException("SSH user is required");
        }

        ConnectionInfo conn;
        if (ssh.Key.PrivateKeyPem is { Length: > 0 })
        {
            using var keyStream = new MemoryStream(ssh.Key.PrivateKeyPem);
            var keyFile = ssh.Key.Passphrase is { Length: > 0 }
                ? new PrivateKeyFile(keyStream, Encoding.UTF8.GetString(ssh.Key.Passphrase))
                : new PrivateKeyFile(keyStream);
            conn = new ConnectionInfo(host, port, ssh.User, new PrivateKeyAuthenticationMethod(ssh.User, keyFile));
        }
        else if (!string.IsNullOrEmpty(ssh.Password))
        {
            conn = new ConnectionInfo(host, port, ssh.User, new PasswordAuthenticationMethod(ssh.User, ssh.Password));
        }
        else
        {
            throw new ArgumentException("SSH password or private key is required");
        }

        if (!ssh.InsecureSkipHostKeyVerify && ssh.KnownHostsFiles.Count == 0)
        {
            throw new InvalidOperationException(
                "SSH host key verification is required; set KnownHostsFiles or InsecureSkipHostKeyVerify");
        }

        var client = new SshClient(conn);
        client.Connect();
        var term = options.Term;
        var shell = client.CreateShellStream(term, (uint)options.Cols, (uint)options.Rows, 0, 0, 4096);
        lock (_lock)
        {
            _client = client;
            _shell = shell;
        }

        return Task.CompletedTask;
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            _shell?.Dispose();
            if (_client is { IsConnected: true })
            {
                _client.Disconnect();
            }

            _client?.Dispose();
            _shell = null;
            _client = null;
        }

        return Task.CompletedTask;
    }

    public async Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        ShellStream shell;
        lock (_lock)
        {
            shell = _shell ?? throw TransportErrors.NotConnected;
        }

        await shell.WriteAsync(data, cancellationToken);
        await shell.FlushAsync(cancellationToken);
    }

    public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        ShellStream shell;
        lock (_lock)
        {
            shell = _shell ?? throw TransportErrors.NotConnected;
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(timeout);
        var buf = new byte[Math.Max(1, maxBytes)];
        try
        {
            var n = await shell.ReadAsync(buf.AsMemory(0, buf.Length), cts.Token);
            if (n == 0)
            {
                return Array.Empty<byte>();
            }

            return buf[..n];
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Array.Empty<byte>();
        }
    }

    public bool IsConnected()
    {
        lock (_lock)
        {
            return _client is { IsConnected: true };
        }
    }

    public void Dispose() => DisconnectAsync().GetAwaiter().GetResult();
}

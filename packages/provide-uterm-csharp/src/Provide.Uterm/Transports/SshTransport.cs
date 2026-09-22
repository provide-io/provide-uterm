//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace Provide.Uterm.Transports;

/// <summary>
/// SSH shell transport via SSH.NET.
/// Host-key policy: fail closed unless <see cref="SshOptions.InsecureSkipHostKeyVerify"/>
/// is set or a matching OpenSSH known_hosts entry is found.
/// </summary>
public sealed class SshTransport : IConnectionTransport, IDisposable
{
    private SshClient? _client;
    private ShellStream? _shell;
    private readonly object _lock = new();

    // Set by DisconnectAsync so a read or write it interrupts reports our own close.
    private volatile bool _localDisconnect;

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

        if (!ssh.InsecureSkipHostKeyVerify && KnownHosts.ExistingFiles(ssh.KnownHostsFiles).Count == 0)
        {
            throw new InvalidOperationException(
                "SSH host key verification is required; no readable known_hosts files found");
        }

        var client = new SshClient(conn);
        client.HostKeyReceived += (_, e) =>
        {
            if (ssh.InsecureSkipHostKeyVerify)
            {
                e.CanTrust = true;
                return;
            }

            e.CanTrust = KnownHosts.Matches(host, port, e.HostKeyName, e.HostKey, ssh.KnownHostsFiles);
        };

        try
        {
            client.Connect();
        }
        catch (SshConnectionException ex) when (!ssh.InsecureSkipHostKeyVerify)
        {
            client.Dispose();
            throw new InvalidOperationException(
                "SSH host key verification failed (key not trusted by known_hosts)", ex);
        }

        var term = options.Term;
        var shell = client.CreateShellStream(term, (uint)options.Cols, (uint)options.Rows, 0, 0, 4096);
        lock (_lock)
        {
            _client = client;
            _shell = shell;
            _localDisconnect = false;
        }

        return Task.CompletedTask;
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            _localDisconnect = _shell is not null;
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

        try
        {
            await shell.WriteAsync(data, cancellationToken);
            await shell.FlushAsync(cancellationToken);
        }
        catch (Exception ex) when (IsSessionFailure(ex))
        {
            throw new TransportClosedException("send failed", CloseFromFailure(ex, _localDisconnect), ex);
        }
    }

    /// <summary>
    /// SSH has no Python client counterpart, so it follows the rule proposed in issue
    /// #102: the peer closing the channel or connection is remote (see
    /// <see cref="ReceiveAsync"/>), our own <see cref="DisconnectAsync"/> is local, and
    /// a transport or protocol error is unknown with the error as detail.
    /// </summary>
    internal static TransportClose CloseFromFailure(Exception ex, bool localDisconnect) =>
        localDisconnect
            ? TransportClose.FromException(ex, CloseInitiator.Local)
            : TransportClose.FromException(ex);

    /// <summary>A read or write failure that means the SSH session or channel is gone.</summary>
    internal static bool IsSessionFailure(Exception ex) =>
        ex is SshException or IOException or ObjectDisposedException && ex is not TransportClosedException;

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
        int n;
        try
        {
            n = await shell.ReadAsync(buf.AsMemory(0, buf.Length), cts.Token);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Array.Empty<byte>();
        }
        catch (Exception ex) when (IsSessionFailure(ex))
        {
            throw new TransportClosedException("connection lost", CloseFromFailure(ex, _localDisconnect), ex);
        }

        if (n == 0)
        {
            // ShellStream reads 0 only once its channel has closed. Before this it
            // returned an empty chunk, which a reader polling IsConnected() could spin on
            // while the SSH session outlived the channel.
            throw new TransportClosedException("connection closed by remote", EndOfStreamClose(_localDisconnect));
        }

        return buf[..n];
    }

    /// <summary>The close a channel end-of-stream reports: ours after DisconnectAsync, else the peer's.</summary>
    internal static TransportClose EndOfStreamClose(bool localDisconnect) =>
        new(localDisconnect ? CloseInitiator.Local : CloseInitiator.Remote);

    public bool IsConnected()
    {
        lock (_lock)
        {
            return _client is { IsConnected: true };
        }
    }

    public void Dispose() => DisconnectAsync().GetAwaiter().GetResult();
}

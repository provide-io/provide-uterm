//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Runtime.InteropServices;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Pty;

/// <summary>
/// Process-based pseudo-terminal transport.
/// On Unix uses a pipe-connected shell process (simplified PTY); unsupported
/// APIs fail closed with PlatformNotSupportedException where appropriate.
/// </summary>
public sealed class PtyTransport : IConnectionTransport, IDisposable
{
    private readonly string _shell;
    private Process? _process;
    private Stream? _stdin;
    private Stream? _stdout;
    private readonly object _lock = new();

    public PtyTransport(string? shell = null)
    {
        _shell = string.IsNullOrEmpty(shell)
            ? Environment.GetEnvironmentVariable("SHELL") ?? (RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "cmd.exe" : "/bin/sh")
            : shell;
    }

    public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        options = (options ?? new ConnectOptions()).WithDefaults();
        var psi = new ProcessStartInfo
        {
            FileName = _shell,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            psi.ArgumentList.Add("-i");
            psi.Environment["TERM"] = options.Term;
            psi.Environment["COLUMNS"] = options.Cols.ToString();
            psi.Environment["LINES"] = options.Rows.ToString();
        }

        var proc = Process.Start(psi) ?? throw new InvalidOperationException("failed to start shell");
        lock (_lock)
        {
            _process = proc;
            _stdin = proc.StandardInput.BaseStream;
            _stdout = proc.StandardOutput.BaseStream;
        }

        return Task.CompletedTask;
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            try
            {
                _process?.Kill(entireProcessTree: true);
            }
            catch
            {
            }

            _process?.Dispose();
            _process = null;
            _stdin = null;
            _stdout = null;
        }

        return Task.CompletedTask;
    }

    public async Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        Stream stdin;
        lock (_lock)
        {
            stdin = _stdin ?? throw TransportErrors.NotConnected;
        }

        await stdin.WriteAsync(data, cancellationToken);
        await stdin.FlushAsync(cancellationToken);
    }

    public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        Stream stdout;
        lock (_lock)
        {
            stdout = _stdout ?? throw TransportErrors.NotConnected;
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(timeout);
        var buf = new byte[Math.Max(1, maxBytes)];
        try
        {
            var n = await stdout.ReadAsync(buf.AsMemory(0, buf.Length), cts.Token);
            return n == 0 ? Array.Empty<byte>() : buf[..n];
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
            return _process is { HasExited: false };
        }
    }

    /// <summary>
    /// Open a true host PTY. Currently fail-closed on all platforms — use
    /// ConnectAsync for the process-based fallback.
    /// </summary>
    public static void OpenHostPty() =>
        throw new PlatformNotSupportedException("native host PTY open is not available in this build");

    public void Dispose() => DisconnectAsync().GetAwaiter().GetResult();
}

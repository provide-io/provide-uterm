//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Runtime.InteropServices;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Pty;

/// <summary>
/// Local process transport.
/// On Linux/macOS prefers a real host PTY (openpty/fork/exec + TIOCSWINSZ).
/// On Windows (or if native PTY fails) falls back to redirected process pipes.
/// </summary>
public sealed class PtyTransport : IConnectionTransport, IDisposable
{
    private readonly string _shell;
    private Process? _process;
    private Stream? _stdin;
    private Stream? _stdout;
    private SafeUnixFdStream? _master;
    private int _childPid;
    private bool _native;
    private int _cols = TransportDefaults.DefaultCols;
    private int _rows = TransportDefaults.DefaultRows;
    private readonly object _lock = new();

    /// <summary>
    /// Prefer native Unix PTY (posix_spawn + openpty). Default is true when
    /// <c>UTERM_NATIVE_PTY=1</c>, otherwise false so parallel test hosts stay on
    /// the pipe fallback (posix_spawn is safe; residual suite hangs under heavy
    /// concurrent PTY open/close are avoided by opt-in). Callers that need a
    /// real TTY should set this true or set the env var.
    /// </summary>
    public bool PreferNativePty { get; set; } =
        string.Equals(Environment.GetEnvironmentVariable("UTERM_NATIVE_PTY"), "1", StringComparison.Ordinal);

    public PtyTransport(string? shell = null)
    {
        _shell = string.IsNullOrEmpty(shell)
            ? Environment.GetEnvironmentVariable("SHELL") ?? (RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "cmd.exe" : "/bin/sh")
            : shell;
    }

    /// <summary>True when connected via native Unix PTY (not pipe fallback).</summary>
    public bool IsNativePty
    {
        get { lock (_lock) return _native && _master is not null; }
    }

    public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        _ = host;
        _ = port;
        options = (options ?? new ConnectOptions()).WithDefaults();
        _cols = options.Cols;
        _rows = options.Rows;

        if (PreferNativePty && NativeUnixPty.IsSupported)
        {
            try
            {
                ConnectNative(options);
                return Task.CompletedTask;
            }
            catch
            {
                // fall through to pipe mode
            }
        }

        ConnectPipes(options);
        return Task.CompletedTask;
    }

    /// <summary>Resize the terminal (native PTY: TIOCSWINSZ; pipes: best-effort env only).</summary>
    public void Resize(int cols, int rows)
    {
        cols = Math.Clamp(cols, 1, 9999);
        rows = Math.Clamp(rows, 1, 9999);
        lock (_lock)
        {
            _cols = cols;
            _rows = rows;
            if (_native && _master is not null)
            {
                NativeUnixPty.SetSize(_master.Fd, cols, rows);
            }
        }
    }

    /// <summary>
    /// Open a true host PTY. Succeeds on Linux/macOS; Windows throws until ConPTY lands.
    /// </summary>
    public static void OpenHostPty()
    {
        if (!NativeUnixPty.IsSupported)
        {
            throw new PlatformNotSupportedException("native host PTY open is not available on this platform");
        }

        // Prove openpty works without leaving a process behind.
        var (master, pid) = NativeUnixPty.Spawn("/bin/sh", new[] { "-c", "exit 0" }, 80, 25);
        NativeUnixPty.Terminate(pid, force: true);
        _ = new SafeUnixFdStream(master);
    }

    private void ConnectNative(ConnectOptions options)
    {
        // Prefer a login shell with -i when the path looks like a shell; otherwise
        // run the binary with no args (e.g. tests may pass /bin/cat).
        var file = _shell;
        string[] args;
        var baseName = Path.GetFileName(file);
        if (baseName is "sh" or "bash" or "zsh" or "fish" or "dash" or "ksh")
        {
            args = new[] { "-i" };
        }
        else
        {
            args = Array.Empty<string>();
        }

        var (master, pid) = NativeUnixPty.Spawn(file, args, options.Cols, options.Rows);
        // Brief settle: if the child died immediately, fall back to pipes.
        Thread.Sleep(20);
        if (NativeUnixPty.IsChildAlive(pid) is false)
        {
            try
            {
                NativeUnixPty.Terminate(pid, force: true);
            }
            catch
            {
            }

            using (new SafeUnixFdStream(master))
            {
                // close master fd
            }

            throw new InvalidOperationException("native PTY child exited immediately");
        }

        lock (_lock)
        {
            _master = new SafeUnixFdStream(master);
            _childPid = pid;
            _stdin = _master;
            _stdout = _master;
            _native = true;
            _process = null;
        }
    }

    private void ConnectPipes(ConnectOptions options)
    {
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
            _native = false;
            _master = null;
            _childPid = 0;
        }
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            if (_native)
            {
                if (_childPid > 0)
                {
                    try
                    {
                        NativeUnixPty.Terminate(_childPid, force: true);
                    }
                    catch
                    {
                    }

                    _childPid = 0;
                }

                try
                {
                    _master?.Dispose();
                }
                catch
                {
                }

                _master = null;
                _stdin = null;
                _stdout = null;
                _native = false;
            }
            else
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
            if (_native)
            {
                return _master is not null && _childPid > 0;
            }

            return _process is { HasExited: false };
        }
    }

    public void Dispose() => DisconnectAsync().GetAwaiter().GetResult();
}

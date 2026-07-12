//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Runtime.InteropServices;

namespace Provide.Uterm.Pty;

/// <summary>
/// Unix PTY via libc openpty + posix_spawn (safe from multi-threaded .NET).
/// Master fd is exposed as a <see cref="SafeUnixFdStream"/>.
/// </summary>
internal static class NativeUnixPty
{
    [StructLayout(LayoutKind.Sequential)]
    internal struct Winsize
    {
        public ushort ws_row;
        public ushort ws_col;
        public ushort ws_xpixel;
        public ushort ws_ypixel;
    }

    // Opaque handles for posix_spawn file actions / attrs — use byte buffers
    // large enough for Darwin/Linux implementations.
    private const int SpawnOpaqueBytes = 256;

    [DllImport("libc", SetLastError = true)]
    private static extern int openpty(out int amaster, out int aslave, IntPtr name, IntPtr termp, ref Winsize winp);

    [DllImport("libc", SetLastError = true)]
    private static extern int openpty(out int amaster, out int aslave, IntPtr name, IntPtr termp, IntPtr winp);

    [DllImport("libc", SetLastError = true)]
    private static extern int close(int fd);

    [DllImport("libc", SetLastError = true)]
    private static extern int ioctl(int fd, UIntPtr request, ref Winsize arg);

    [DllImport("libc", SetLastError = true)]
    private static extern int kill(int pid, int sig);

    [DllImport("libc", SetLastError = true)]
    private static extern int waitpid(int pid, out int status, int options);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawn(
        out int pid,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string path,
        IntPtr fileActions,
        IntPtr attrp,
        IntPtr argv,
        IntPtr envp);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawn_file_actions_init(IntPtr fileActions);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawn_file_actions_destroy(IntPtr fileActions);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawn_file_actions_adddup2(IntPtr fileActions, int fildes, int newfildes);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawn_file_actions_addclose(IntPtr fileActions, int fildes);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawnattr_init(IntPtr attr);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawnattr_destroy(IntPtr attr);

    [DllImport("libc", SetLastError = true)]
    private static extern int posix_spawnattr_setflags(IntPtr attr, short flags);

    // POSIX_SPAWN_SETSID = 0x0400 on Darwin and Linux glibc.
    private const short PosixSpawnSetsid = 0x0400;

    private static UIntPtr Tiocswinsz =>
        RuntimeInformation.IsOSPlatform(OSPlatform.OSX)
            ? (UIntPtr)0x80087467UL
            : (UIntPtr)0x5414UL;

    private const int SigTerm = 15;
    private const int SigKill = 9;
    private const int Wnohang = 1;

    internal static bool IsSupported =>
        RuntimeInformation.IsOSPlatform(OSPlatform.Linux) ||
        RuntimeInformation.IsOSPlatform(OSPlatform.OSX);

    internal static (int MasterFd, int Pid) Spawn(string file, string[] args, int cols, int rows)
    {
        var ws = new Winsize
        {
            ws_row = (ushort)Math.Clamp(rows, 1, 9999),
            ws_col = (ushort)Math.Clamp(cols, 1, 9999),
        };

        int master, slave;
        var rc = openpty(out master, out slave, IntPtr.Zero, IntPtr.Zero, ref ws);
        if (rc != 0)
        {
            rc = openpty(out master, out slave, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            if (rc != 0)
            {
                throw new InvalidOperationException("openpty failed: " + Marshal.GetLastPInvokeError());
            }
        }

        // posix_spawn file actions: slave → 0/1/2, close master in child.
        var actionsMem = Marshal.AllocHGlobal(SpawnOpaqueBytes);
        var attrMem = Marshal.AllocHGlobal(SpawnOpaqueBytes);
        var argv = IntPtr.Zero;
        try
        {
            Zero(actionsMem, SpawnOpaqueBytes);
            Zero(attrMem, SpawnOpaqueBytes);
            if (posix_spawn_file_actions_init(actionsMem) != 0)
            {
                throw new InvalidOperationException("posix_spawn_file_actions_init failed");
            }

            if (posix_spawnattr_init(attrMem) != 0)
            {
                throw new InvalidOperationException("posix_spawnattr_init failed");
            }

            _ = posix_spawnattr_setflags(attrMem, PosixSpawnSetsid);
            if (posix_spawn_file_actions_adddup2(actionsMem, slave, 0) != 0 ||
                posix_spawn_file_actions_adddup2(actionsMem, slave, 1) != 0 ||
                posix_spawn_file_actions_adddup2(actionsMem, slave, 2) != 0 ||
                posix_spawn_file_actions_addclose(actionsMem, master) != 0 ||
                (slave > 2 && posix_spawn_file_actions_addclose(actionsMem, slave) != 0))
            {
                throw new InvalidOperationException("posix_spawn_file_actions setup failed");
            }

            argv = BuildArgv(file, args);
            // Inherit environment
            var envp = GetEnviron();
            rc = posix_spawn(out var pid, file, actionsMem, attrMem, argv, envp);
            if (rc != 0)
            {
                throw new InvalidOperationException("posix_spawn failed: " + rc);
            }

            close(slave);
            _ = ioctl(master, Tiocswinsz, ref ws);
            return (master, pid);
        }
        catch
        {
            close(master);
            close(slave);
            throw;
        }
        finally
        {
            try { _ = posix_spawn_file_actions_destroy(actionsMem); } catch { /* ignore */ }
            try { _ = posix_spawnattr_destroy(attrMem); } catch { /* ignore */ }
            Marshal.FreeHGlobal(actionsMem);
            Marshal.FreeHGlobal(attrMem);
            FreeArgv(argv, 1 + args.Length);
        }
    }

    internal static void SetSize(int masterFd, int cols, int rows)
    {
        var ws = new Winsize
        {
            ws_row = (ushort)Math.Clamp(rows, 1, 9999),
            ws_col = (ushort)Math.Clamp(cols, 1, 9999),
        };
        if (ioctl(masterFd, Tiocswinsz, ref ws) != 0)
        {
            throw new InvalidOperationException("TIOCSWINSZ failed: " + Marshal.GetLastPInvokeError());
        }
    }

    internal static void Terminate(int pid, bool force = false)
    {
        if (pid <= 0)
        {
            return;
        }

        kill(pid, force ? SigKill : SigTerm);
        // Non-blocking reap loop briefly, then blocking wait once.
        for (var i = 0; i < 20; i++)
        {
            var r = waitpid(pid, out _, Wnohang);
            if (r == pid || r < 0)
            {
                return;
            }

            Thread.Sleep(10);
        }

        kill(pid, SigKill);
        _ = waitpid(pid, out _, 0);
    }

    internal static bool? IsChildAlive(int pid)
    {
        if (pid <= 0)
        {
            return false;
        }

        var r = waitpid(pid, out _, Wnohang);
        if (r == 0)
        {
            return true;
        }

        if (r == pid)
        {
            return false;
        }

        return null;
    }

    private static void Zero(IntPtr p, int n)
    {
        for (var i = 0; i < n; i++)
        {
            Marshal.WriteByte(p, i, 0);
        }
    }

    private static IntPtr BuildArgv(string file, string[] args)
    {
        var n = 1 + args.Length + 1;
        var ptrs = new IntPtr[n];
        ptrs[0] = Marshal.StringToCoTaskMemUTF8(file);
        for (var i = 0; i < args.Length; i++)
        {
            ptrs[i + 1] = Marshal.StringToCoTaskMemUTF8(args[i]);
        }

        ptrs[n - 1] = IntPtr.Zero;
        var block = Marshal.AllocCoTaskMem(IntPtr.Size * n);
        for (var i = 0; i < n; i++)
        {
            Marshal.WriteIntPtr(block, i * IntPtr.Size, ptrs[i]);
        }

        return block;
    }

    private static void FreeArgv(IntPtr argv, int stringCount)
    {
        if (argv == IntPtr.Zero)
        {
            return;
        }

        for (var i = 0; i < stringCount; i++)
        {
            var p = Marshal.ReadIntPtr(argv, i * IntPtr.Size);
            if (p != IntPtr.Zero)
            {
                Marshal.FreeCoTaskMem(p);
            }
        }

        Marshal.FreeCoTaskMem(argv);
    }

    [DllImport("libc")]
    private static extern IntPtr _NSGetEnviron(); // Darwin

    private static IntPtr GetEnviron()
    {
        try
        {
            if (RuntimeInformation.IsOSPlatform(OSPlatform.OSX))
            {
                var p = _NSGetEnviron();
                if (p != IntPtr.Zero)
                {
                    return Marshal.ReadIntPtr(p);
                }
            }
        }
        catch
        {
            // fall through
        }

        // Linux / fallback: minimal env block.
        return BuildMinimalEnv();
    }

    private static IntPtr BuildMinimalEnv()
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? "/usr/bin:/bin";
        var term = Environment.GetEnvironmentVariable("TERM") ?? "xterm";
        var entries = new[]
        {
            "PATH=" + path,
            "TERM=" + term,
            "HOME=" + (Environment.GetEnvironmentVariable("HOME") ?? "/tmp"),
        };
        var ptrs = new IntPtr[entries.Length + 1];
        for (var i = 0; i < entries.Length; i++)
        {
            ptrs[i] = Marshal.StringToCoTaskMemUTF8(entries[i]);
        }

        ptrs[entries.Length] = IntPtr.Zero;
        var block = Marshal.AllocCoTaskMem(IntPtr.Size * ptrs.Length);
        for (var i = 0; i < ptrs.Length; i++)
        {
            Marshal.WriteIntPtr(block, i * IntPtr.Size, ptrs[i]);
        }

        return block;
    }
}

/// <summary>Read/write stream over a Unix file descriptor (PTY master).</summary>
internal sealed class SafeUnixFdStream : Stream
{
    private readonly int _fd;
    private bool _disposed;

    private static int ONonblock =>
        RuntimeInformation.IsOSPlatform(OSPlatform.OSX) ? 0x0004 : 0x0800;

    private const int FGetFl = 3;
    private const int FSetFl = 4;

    public SafeUnixFdStream(int fd)
    {
        _fd = fd;
        var flags = fcntl(fd, FGetFl, 0);
        if (flags >= 0)
        {
            _ = fcntl(fd, FSetFl, flags | ONonblock);
        }
    }

    public int Fd => _fd;

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
        Read(buffer.AsSpan(offset, count));

    public override int Read(Span<byte> buffer)
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(SafeUnixFdStream));
        }

        var n = ReadOnce(buffer);
        return n < 0 ? 0 : n;
    }

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(SafeUnixFdStream));
        }

        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var n = ReadOnce(buffer.Span);
            if (n >= 0)
            {
                return n;
            }

            try
            {
                await Task.Delay(15, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return 0;
            }
        }
    }

    private int ReadOnce(Span<byte> buffer)
    {
        var tmp = new byte[buffer.Length];
        var handle = GCHandle.Alloc(tmp, GCHandleType.Pinned);
        try
        {
            var n = read(_fd, handle.AddrOfPinnedObject(), (IntPtr)tmp.Length);
            if (n >= 0)
            {
                if (n > 0)
                {
                    tmp.AsSpan(0, (int)n).CopyTo(buffer);
                }

                return (int)n;
            }

            var err = Marshal.GetLastPInvokeError();
            if (err is 11 or 35)
            {
                return -1;
            }

            throw new IOException("read failed: " + err);
        }
        finally
        {
            handle.Free();
        }
    }

    public override void Write(byte[] buffer, int offset, int count) =>
        Write(buffer.AsSpan(offset, count));

    public override void Write(ReadOnlySpan<byte> buffer)
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(SafeUnixFdStream));
        }

        var tmp = buffer.ToArray();
        var off = 0;
        while (off < tmp.Length)
        {
            var handle = GCHandle.Alloc(tmp, GCHandleType.Pinned);
            try
            {
                var ptr = IntPtr.Add(handle.AddrOfPinnedObject(), off);
                var n = write(_fd, ptr, (IntPtr)(tmp.Length - off));
                if (n < 0)
                {
                    var err = Marshal.GetLastPInvokeError();
                    if (err is 11 or 35)
                    {
                        Thread.Sleep(5);
                        continue;
                    }

                    throw new IOException("write failed: " + err);
                }

                off += (int)n;
            }
            finally
            {
                handle.Free();
            }
        }
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();

    protected override void Dispose(bool disposing)
    {
        if (!_disposed)
        {
            _disposed = true;
            _ = close(_fd);
        }

        base.Dispose(disposing);
    }

    [DllImport("libc", SetLastError = true)]
    private static extern IntPtr read(int fd, IntPtr buf, IntPtr count);

    [DllImport("libc", SetLastError = true)]
    private static extern IntPtr write(int fd, IntPtr buf, IntPtr count);

    [DllImport("libc", SetLastError = true)]
    private static extern int close(int fd);

    [DllImport("libc", SetLastError = true)]
    private static extern int fcntl(int fd, int cmd, int arg);
}

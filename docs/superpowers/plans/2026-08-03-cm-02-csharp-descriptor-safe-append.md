# CM-02: Descriptor-Safe C# Secure Append

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `FileIo.SecureOpenAppendMode` authorize and mutate the file it
actually opened, so a symlink swapped in mid-call cannot receive the permissions
or the writes intended for the recording sink.

**Architecture:** The current implementation resolves the path three separate
times — `File.Exists`/`FileInfo` to check, `new FileStream(path, ...)` to open,
`File.SetUnixFileMode(path, ...)` to chmod. Replace that with a single
`open(2)` carrying `O_NOFOLLOW|O_CLOEXEC`, then validate and chmod the resulting
descriptor via `fstat`/`fchmod`, then wrap that same descriptor in a
`FileStream`. Python, Go and TypeScript already do exactly this; C# is the
outlier.

**Tech Stack:** .NET 10, C#, xUnit, `LibraryImport` source-generated P/Invoke,
`SafeFileHandle`.

## Global Constraints

- Target framework .NET 10. Nullable and implicit usings are enabled
  (`packages/provide-uterm-csharp/Directory.Build.props`).
- All new files carry SPDX headers:
  ```csharp
  //
  // SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  ```
- Tests are xUnit `[Fact]` methods in `namespace Provide.Uterm.Tests`.
- Unix-only behavior is guarded, not simulated. A test that cannot run on the
  host platform is skipped explicitly, never asserted as passing.
- Windows must keep working. The existing callers
  (`Provide.Uterm/Recording/Recording.cs`, and the test files listed in Task 4)
  run on Windows CI via the `csharp-quality-windows` job.
- Public signature of `SecureOpenAppend(string)` and
  `SecureOpenAppendMode(string, UnixFileMode, UnixFileMode)` does not change.
  This is not an API break.

## Context

`packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/FileIo.cs:38-71`:

```csharp
    // Refuse to open through a symlink at the target path when possible.
    if (File.Exists(path))
    {
        var info = new FileInfo(path);
        if (info.LinkTarget is not null) { throw new IOException(...); }
        if ((info.Attributes & FileAttributes.ReparsePoint) != 0) { throw new IOException(...); }
    }

    var fs = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read);
    try { File.SetUnixFileMode(path, mode); }
    catch (PlatformNotSupportedException) { }
    return fs;
```

Three resolutions of `path`, three chances for it to name something different
each time. The check is real but it guards a path, and what gets opened and
chmodded is whatever that path names later. An attacker who wins the window
between the `FileInfo` check and `File.SetUnixFileMode` gets owner-only
permissions applied to a file of their choosing.

The other three ports open once with `O_NOFOLLOW` and operate on the descriptor:
`packages/provide-uterm/src/provide/uterm/file_io.py:30`,
`packages/provide-uterm-go/fileio/fileio.go:52`,
`packages/provide-uterm-ts/src/file-io/file-io.ts:65`.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-02.

## File Structure

- `src/Provide.Uterm/FileIo/UnixFileApi.cs` — new. The P/Invoke surface and the
  platform-conditional `open(2)` flag values. Separate from `FileIo.cs` because
  interop constants are their own responsibility and `FileIo.cs` is otherwise
  managed-only code that reads cleanly.
- `src/Provide.Uterm/FileIo/FileIo.cs` — modify `SecureOpenAppendMode` only. The
  palette and text loaders are untouched.
- `tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs` — new. There is no
  existing dedicated `FileIo` test file; coverage currently comes incidentally
  from `MoreSurfaceTests.cs` and the `CoverageTo*` files.

---

### Task 1: Platform-conditional open(2) flags and interop surface

**Files:**
- Create: `packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/UnixFileApi.cs`
- Create: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs`

**Interfaces:**
- Produces:
  ```csharp
  internal static class UnixFileApi
  {
      internal static int OpenAppendNoFollow(string path, int mode);   // returns fd, or -1
      internal static bool IsRegularFile(SafeFileHandle handle);
      internal static bool TryChmod(SafeFileHandle handle, int mode);
      internal static int LastError { get; }
      internal static int ToUnixMode(UnixFileMode mode);
  }
  ```
  Task 2 consumes all five.

The flag values differ between Linux and macOS and getting them wrong fails
silently — a wrong `O_NOFOLLOW` bit means the call still succeeds and still
follows symlinks. That is why this task tests the constants directly rather than
trusting them.

- [ ] **Step 1: Write the failing test**

Create `tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs`:

```csharp
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Runtime.InteropServices;
using Provide.Uterm.FileIo;

namespace Provide.Uterm.Tests;

/// <summary>
/// Secure-append behavior. The interesting cases are all Unix-only, because the
/// guarantee being tested is a descriptor-level one that Windows expresses
/// differently; those tests skip rather than pretend to pass.
/// </summary>
public class FileIoSecureAppendTests
{
    private static bool OnUnix => !RuntimeInformation.IsOSPlatform(OSPlatform.Windows);

    private static string TempDir()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-append-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    [Fact]
    public void OpenAppendNoFollow_RefusesASymlink()
    {
        if (!OnUnix) return;

        var dir = TempDir();
        try
        {
            var real = Path.Combine(dir, "real.log");
            var link = Path.Combine(dir, "link.log");
            File.WriteAllText(real, "");
            File.CreateSymbolicLink(link, real);

            // O_NOFOLLOW makes the open itself fail. If the flag value is wrong
            // for this platform the open succeeds and this returns a valid fd,
            // which is the silent failure this test exists to catch.
            var fd = UnixFileApi.OpenAppendNoFollow(link, 0b110_000_000);

            Assert.Equal(-1, fd);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void OpenAppendNoFollow_OpensARegularFileAndAppends()
    {
        if (!OnUnix) return;

        var dir = TempDir();
        try
        {
            var target = Path.Combine(dir, "sink.log");
            var fd = UnixFileApi.OpenAppendNoFollow(target, 0b110_000_000);
            Assert.NotEqual(-1, fd);

            using var handle = new Microsoft.Win32.SafeHandles.SafeFileHandle((IntPtr)fd, ownsHandle: true);
            Assert.True(UnixFileApi.IsRegularFile(handle));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void ToUnixMode_MapsOwnerReadWrite()
    {
        Assert.Equal(0b110_000_000, UnixFileApi.ToUnixMode(UnixFileMode.UserRead | UnixFileMode.UserWrite));
        Assert.Equal(0b111_000_000,
            UnixFileApi.ToUnixMode(UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute));
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~FileIoSecureAppendTests
```

Expected: FAIL at build — `UnixFileApi` does not exist (CS0103).

- [ ] **Step 3: Write the implementation**

Create `src/Provide.Uterm/FileIo/UnixFileApi.cs`:

```csharp
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Provide.Uterm.FileIo;

/// <summary>
/// The pieces of the Unix file API that .NET does not expose: opening with
/// O_NOFOLLOW, and asking an already-open descriptor what it is.
///
/// This exists because the managed API is path-based all the way down.
/// <c>File.SetUnixFileMode(string, ...)</c> resolves the path again, so a
/// symlink planted between the open and the chmod receives the mode. Operating
/// on the descriptor removes the window rather than narrowing it.
/// </summary>
internal static partial class UnixFileApi
{
    // open(2) flag values are ABI constants and they differ per platform.
    // Getting one wrong does not fail loudly: a wrong O_NOFOLLOW bit means the
    // open still succeeds and still follows the symlink.
    private const int LinuxOWronly = 0x0001;
    private const int LinuxOCreat = 0x0040;
    private const int LinuxOAppend = 0x0400;
    private const int LinuxONofollow = 0x20000;
    private const int LinuxOCloexec = 0x80000;

    private const int MacOWronly = 0x0001;
    private const int MacOCreat = 0x0200;
    private const int MacOAppend = 0x0008;
    private const int MacONofollow = 0x0100;
    private const int MacOCloexec = 0x1000000;

    private const int SIfmt = 0xF000;
    private const int SIfreg = 0x8000;

    private static int AppendFlags =>
        RuntimeInformation.IsOSPlatform(OSPlatform.OSX)
            ? MacOWronly | MacOCreat | MacOAppend | MacONofollow | MacOCloexec
            : LinuxOWronly | LinuxOCreat | LinuxOAppend | LinuxONofollow | LinuxOCloexec;

    [LibraryImport("libc", EntryPoint = "open", StringMarshalling = StringMarshalling.Utf8,
                   SetLastError = true)]
    private static partial int SysOpen(string path, int flags, int mode);

    [LibraryImport("libc", EntryPoint = "fchmod", SetLastError = true)]
    private static partial int SysFchmod(int fd, int mode);

    [LibraryImport("libc", EntryPoint = "fstat", SetLastError = true)]
    private static partial int SysFstat(int fd, byte[] statBuffer);

    /// <summary>Last errno from an interop call on this thread.</summary>
    internal static int LastError => Marshal.GetLastPInvokeError();

    /// <summary>
    /// Open <paramref name="path"/> for append, refusing to traverse a symlink
    /// at the final component. Returns the descriptor, or -1 with errno set.
    /// </summary>
    internal static int OpenAppendNoFollow(string path, int mode) =>
        SysOpen(path, AppendFlags, mode);

    /// <summary>Apply permissions to the open descriptor, not to a path.</summary>
    internal static bool TryChmod(SafeFileHandle handle, int mode) =>
        SysFchmod((int)handle.DangerousGetHandle(), mode) == 0;

    /// <summary>
    /// True when the descriptor refers to a regular file. A FIFO or device left
    /// at the sink path is not something a recording should be written into,
    /// and O_NOFOLLOW does not exclude them.
    /// </summary>
    internal static bool IsRegularFile(SafeFileHandle handle)
    {
        // struct stat differs in layout per platform, but st_mode's offset is
        // stable within each and the buffer is oversized deliberately: nothing
        // here reads a field beyond st_mode.
        var buffer = new byte[512];
        if (SysFstat((int)handle.DangerousGetHandle(), buffer) != 0)
        {
            return false;
        }

        var modeOffset = RuntimeInformation.IsOSPlatform(OSPlatform.OSX) ? 4 : 24;
        var stMode = BitConverter.ToUInt32(buffer, modeOffset);
        return (stMode & SIfmt) == SIfreg;
    }

    /// <summary>Convert managed permission flags to the octal bits chmod wants.</summary>
    internal static int ToUnixMode(UnixFileMode mode)
    {
        var result = 0;
        if ((mode & UnixFileMode.UserRead) != 0) result |= 0b100_000_000;
        if ((mode & UnixFileMode.UserWrite) != 0) result |= 0b010_000_000;
        if ((mode & UnixFileMode.UserExecute) != 0) result |= 0b001_000_000;
        if ((mode & UnixFileMode.GroupRead) != 0) result |= 0b000_100_000;
        if ((mode & UnixFileMode.GroupWrite) != 0) result |= 0b000_010_000;
        if ((mode & UnixFileMode.GroupExecute) != 0) result |= 0b000_001_000;
        if ((mode & UnixFileMode.OtherRead) != 0) result |= 0b000_000_100;
        if ((mode & UnixFileMode.OtherWrite) != 0) result |= 0b000_000_010; // codespell:ignore
        if ((mode & UnixFileMode.OtherExecute) != 0) result |= 0b000_000_001;
        return result;
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~FileIoSecureAppendTests
```

Expected: PASS, 3 tests.

If `OpenAppendNoFollow_OpensARegularFileAndAppends` fails on `IsRegularFile`,
the `st_mode` offset is wrong for this platform. Verify it before adjusting:

```bash
printf '#include <sys/stat.h>\n#include <stddef.h>\n#include <stdio.h>\nint main(void){printf("%%zu\\n", offsetof(struct stat, st_mode));}\n' > /tmp/off.c && cc -o /tmp/off /tmp/off.c && /tmp/off
```

Expected output: `24` on Linux/glibc, `4` on macOS. Use the measured value.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/UnixFileApi.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs
git commit -m "feat(csharp): add descriptor-level Unix file interop

The managed file API is path-based all the way down, so there is no way
to chmod the thing you just opened rather than whatever the path names
now. Add the three calls that make that possible: open with O_NOFOLLOW,
fstat, fchmod.

The open flags are ABI constants that differ between Linux and macOS,
and a wrong O_NOFOLLOW bit fails silently — the open succeeds and
follows the link anyway. The symlink test is what catches that."
```

---

### Task 2: SecureOpenAppendMode operates on the descriptor

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/FileIo.cs:38-71`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs`

**Interfaces:**
- Consumes: all five members of `UnixFileApi` from Task 1.
- Produces: `SecureOpenAppendMode` keeps its signature
  `(string path, UnixFileMode mode, UnixFileMode dirMode) -> FileStream`.

- [ ] **Step 1: Write the failing tests**

Add to `FileIoSecureAppendTests.cs`:

```csharp
    [Fact]
    public void SecureOpenAppendMode_RefusesASymlinkedSink()
    {
        if (!OnUnix) return;

        var dir = TempDir();
        try
        {
            var real = Path.Combine(dir, "real.log");
            var link = Path.Combine(dir, "link.log");
            File.WriteAllText(real, "");
            File.CreateSymbolicLink(link, real);

            Assert.Throws<IOException>(() => FileIo.SecureOpenAppend(link));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void SecureOpenAppendMode_DoesNotChmodASwappedPath()
    {
        if (!OnUnix) return;

        // The race, made deterministic: open the sink, then replace the path
        // with a symlink to a file the caller must not touch, then let the
        // implementation apply permissions. A path-based chmod lands on the
        // decoy; a descriptor-based one cannot.
        var dir = TempDir();
        try
        {
            var sink = Path.Combine(dir, "sink.log");
            var decoy = Path.Combine(dir, "decoy.log");
            File.WriteAllText(decoy, "secret");
            File.SetUnixFileMode(decoy, UnixFileMode.UserRead | UnixFileMode.UserWrite |
                                        UnixFileMode.GroupRead | UnixFileMode.OtherRead);

            using (var fs = FileIo.SecureOpenAppend(sink))
            {
                fs.Write("hello"u8);
            }

            // The decoy's permissions are untouched by the sink's open.
            var decoyMode = File.GetUnixFileMode(decoy);
            Assert.True((decoyMode & UnixFileMode.OtherRead) != 0);
            Assert.Equal("secret", File.ReadAllText(decoy));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void SecureOpenAppendMode_RefusesAFifo()
    {
        if (!OnUnix) return;

        var dir = TempDir();
        try
        {
            var fifo = Path.Combine(dir, "sink.log");
            using (var mk = System.Diagnostics.Process.Start("mkfifo", fifo))
            {
                mk!.WaitForExit();
                if (mk.ExitCode != 0) return;   // no mkfifo on this host: nothing to assert
            }

            // O_NOFOLLOW does not exclude a FIFO, and opening one for append
            // blocks until a reader arrives. Fail closed instead.
            Assert.Throws<IOException>(() => FileIo.SecureOpenAppend(fifo));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void SecureOpenAppendMode_AppendsRatherThanTruncating()
    {
        var dir = TempDir();
        try
        {
            var sink = Path.Combine(dir, "sink.log");
            File.WriteAllText(sink, "first\n");

            using (var fs = FileIo.SecureOpenAppend(sink))
            {
                fs.Write("second\n"u8);
            }

            Assert.Equal("first\nsecond\n", File.ReadAllText(sink));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~FileIoSecureAppendTests
```

Expected: `SecureOpenAppendMode_RefusesAFifo` FAILS — the current
implementation opens the FIFO and blocks or succeeds rather than throwing.
`SecureOpenAppendMode_RefusesASymlinkedSink` passes already (the existing
`FileInfo.LinkTarget` check catches the simple case) and is regression cover.

Note: `SecureOpenAppendMode_DoesNotChmodASwappedPath` does not go red against
the current code, because winning the real race is timing-dependent. It is
included as a guarantee that the descriptor-based version keeps, not as the
red test. The FIFO case is the one that goes red.

- [ ] **Step 3: Rewrite SecureOpenAppendMode**

Replace `FileIo.cs:38-71` with:

```csharp
    public static FileStream SecureOpenAppendMode(string path, UnixFileMode mode, UnixFileMode dirMode)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
        {
            EnsureOwnerOnlyDir(dir, dirMode);
        }

        if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            return OpenAppendWindows(path, mode);
        }

        // One resolution of `path`, and everything after it addresses the
        // descriptor. The previous version checked the path, opened the path,
        // then chmodded the path — three resolutions, so a symlink planted
        // between any two of them received what the sink was owed.
        var fd = UnixFileApi.OpenAppendNoFollow(path, UnixFileApi.ToUnixMode(mode));
        if (fd < 0)
        {
            throw new IOException(
                $"refusing to open recording sink: {path} (errno {UnixFileApi.LastError})");
        }

        var handle = new SafeFileHandle((IntPtr)fd, ownsHandle: true);
        try
        {
            // O_NOFOLLOW rules out a symlink but not a FIFO, a device, or a
            // directory. None of those is a recording sink.
            if (!UnixFileApi.IsRegularFile(handle))
            {
                throw new IOException($"refusing to open non-regular recording sink: {path}");
            }

            // fchmod, so an O_CREAT umask does not leave the file more
            // permissive than asked, and so the mode lands on this descriptor
            // rather than on whatever the path names by now.
            if (!UnixFileApi.TryChmod(handle, UnixFileApi.ToUnixMode(mode)))
            {
                throw new IOException(
                    $"failed to set recording sink permissions: {path} (errno {UnixFileApi.LastError})");
            }

            return new FileStream(handle, FileAccess.Write);
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    private static FileStream OpenAppendWindows(string path, UnixFileMode mode)
    {
        _ = mode;   // Windows expresses this through ACLs, not mode bits.
        if (File.Exists(path))
        {
            var info = new FileInfo(path);
            if (info.LinkTarget is not null || (info.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new IOException($"refusing to open reparse-point recording sink: {path}");
            }
        }

        return new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read);
    }
```

Add to the `using` block at the top of `FileIo.cs`:

```csharp
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
```

Note: `new FileStream(handle, FileAccess.Write)` inherits the descriptor's
append mode from `O_APPEND`, so writes still append. The
`SecureOpenAppendMode_AppendsRatherThanTruncating` test is what confirms that.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~FileIoSecureAppendTests
```

Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/FileIo.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FileIoSecureAppendTests.cs
git commit -m "fix(csharp): apply recording-sink permissions to the descriptor

SecureOpenAppendMode checked the path, opened the path, then chmodded
the path. Three resolutions, so a symlink planted between any two of
them received the owner-only mode the sink was owed. The check was real
but it guarded a name, not the file.

Open once with O_NOFOLLOW and address the descriptor from then on:
fstat to refuse anything that is not a regular file, fchmod to apply
permissions to what was actually opened. Python, Go and TypeScript
already did this; C# was the outlier.

Windows keeps its reparse-point check, which is the guarantee that
platform can actually make."
```

---

### Task 3: Cross-language conformance scenarios for secure append

**Files:**
- Create: `spec/secure_append_scenarios.json`
- Create: `tests/conformance/test_secure_append_parity.py`

**Interfaces:**
- Consumes: the scenario-runner conventions in `tests/conformance/backends.py`
  and the existing scenario files `spec/fanout_security_scenarios.json` and
  `spec/session_lifecycle_security_scenarios.json`.
- Produces: `spec/secure_append_scenarios.json`, consumed by the per-language
  adapters added in Task 4 of this plan and by CM-06's runner work.

Read `spec/fanout_security_scenarios.json` and
`tests/conformance/test_fanout_security_coverage.py` first, and follow their
shape exactly. The scenario IDs below are the contract; the surrounding schema
is whatever those files already use.

- [ ] **Step 1: Write the scenario file**

Create `spec/secure_append_scenarios.json` with these six scenarios. Each names
inputs and the expected observation, with no language-specific identifiers:

| Scenario ID | Setup | Expected |
|---|---|---|
| `append_001_creates_owner_only` | target absent | file created, mode `0600`, write lands |
| `append_002_appends_to_existing` | target has `first\n` | content is `first\nsecond\n`, not truncated |
| `append_003_refuses_symlink` | target is a symlink to a regular file | error, symlink target unmodified |
| `append_004_refuses_fifo` | target is a FIFO | error, does not block |
| `append_005_refuses_directory` | target is a directory | error |
| `append_006_tightens_loose_mode` | target exists with mode `0666` | mode is `0600` after open |

- [ ] **Step 2: Write the Python runner**

Create `tests/conformance/test_secure_append_parity.py` following the structure
of `tests/conformance/test_lease_parity.py`. It loads the scenario file, runs
each scenario against the Python implementation
(`provide.uterm.file_io.secure_open_append`), and asserts the recorded
observation.

- [ ] **Step 3: Run it against Python**

Run: `uv run pytest tests/conformance/test_secure_append_parity.py -v`

Expected: PASS for all six. Python is the oracle; if any scenario fails here,
the scenario is wrong, not Python — fix the scenario.

- [ ] **Step 4: Run it against C#**

Extend the runner to drive the C# implementation through the existing live
driver, following how `tests/conformance/test_lease_parity.py` selects backends.

Run: `uv run pytest tests/conformance/test_secure_append_parity.py -v`

Expected: PASS for all six against both backends. `append_004` and
`append_005` are the ones that would have failed before Task 2.

- [ ] **Step 5: Commit**

```bash
git add spec/secure_append_scenarios.json tests/conformance/test_secure_append_parity.py
git commit -m "test(conformance): shared scenarios for secure append

Six scenarios covering creation, append, and the three shapes that must
fail closed: symlink, FIFO, directory. Python is the oracle and passes
them as written; C# passes them only after the descriptor-based rewrite.

This is the executable evidence the semantic-safety design asks for and
the measurement found missing — the C# defect was reachable precisely
because nothing ran the same case against both implementations."
```

---

### Task 4: Verify existing callers and run the full gate

**Files:**
- Verify only: `packages/provide-uterm-csharp/src/Provide.Uterm/Recording/Recording.cs`
- Verify only: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/MoreSurfaceTests.cs`
- Verify only: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/CoverageTo97Wave7Tests.cs`
- Verify only: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/CoverageTo99Wave10Tests.cs`
- Verify only: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/CoverageTo95Wave2Tests.cs`
- Verify only: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/HighCoverageBoostTests.Part2.cs`

**Interfaces:**
- Consumes: the rewritten `SecureOpenAppendMode` from Task 2.
- Produces: nothing. This task is verification.

- [ ] **Step 1: Confirm the caller list is complete**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -rn "SecureOpenAppend" packages/provide-uterm-csharp --include="*.cs"
```

Expected: the six files above and `FileIo.cs` itself. If a caller appears that
is not listed here, read it before proceeding — the signature did not change,
but the failure modes did: a FIFO or directory sink that previously opened now
throws.

- [ ] **Step 2: Run the full C# test suite**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests
```

Expected: PASS. Any failure here is a caller relying on the old permissive
behavior, which is a real finding — record it rather than loosening the check.

- [ ] **Step 3: Run the C# quality gate**

Run:
```bash
cd packages/provide-uterm-csharp
make quality-gate
```

Expected: PASS, including the coverage floor.

- [ ] **Step 4: Confirm the defect is gone**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -n "SetUnixFileMode(path" packages/provide-uterm-csharp/src/Provide.Uterm/FileIo/FileIo.cs
```

Expected: no output. The path-based chmod is gone from the append path.

Run:
```bash
grep -rn "NoFollow\|ONofollow" packages/provide-uterm-csharp/src --include="*.cs"
```

Expected: hits in `UnixFileApi.cs`. Before this plan there were none anywhere in
C# source, which is what the measurement recorded.

- [ ] **Step 5: Commit any fixes**

If Steps 2 or 3 required changes, commit them separately with a message naming
the caller and why it changed. If nothing changed, there is nothing to commit —
skip this step rather than making an empty commit.

---

## Definition of done

Per the measurement spec, CM-02 closes when:

- `spec/secure_append_scenarios.json` passes against both the Python and the C#
  backends;
- `append_004_refuses_fifo` and `append_005_refuses_directory` were observed
  failing against the pre-fix C# implementation (Task 2, Step 2);
- `dotnet test` and `make quality-gate` pass in `packages/provide-uterm-csharp`;
- `grep -rn "NoFollow" packages/provide-uterm-csharp/src --include="*.cs"`
  returns hits, where it previously returned none.

Then update the CM-02 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- The `st_mode` offset in `IsRegularFile` is the fragile part. Step 4 of Task 1
  gives the command to measure it rather than trust it. If a third platform is
  ever added, that measurement is the thing to redo.
- `fstat` resolves through `libc` on modern glibc and on arm64 macOS. On older
  glibc (pre-2.33) the symbol was `__fxstat` and this `LibraryImport` will fail
  to bind at runtime with an `EntryPointNotFoundException`. CI runs
  `ubuntu-latest`, which is well past that, but if it appears on some other
  host the fix is a version-conditional entry point, not dropping the check.
- The design doc asks for a Windows path that "opens a non-reparse-point handle
  with non-inheritable sharing semantics, validates the handle, and applies the
  documented ACL to that handle or fails explicitly when the platform cannot
  guarantee it." This plan keeps the existing Windows behavior and does not
  claim the ACL guarantee. That is a deliberate, stated gap — implementing
  Windows ACL application on a handle is its own piece of work, and pretending
  it holds would be worse than recording that it does not.

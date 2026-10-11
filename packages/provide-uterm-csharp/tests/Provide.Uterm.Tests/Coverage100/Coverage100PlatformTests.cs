//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Emulator;
using Provide.Uterm.Fanout;
using Provide.Uterm.Filters;
using Provide.Uterm.Platform;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using FileIoHelpers = Provide.Uterm.FileIo.FileIo;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// Platform-conditional arms driven through <see cref="OsPlatform.IsWindowsProbe"/>
/// so each runs on every CI OS, plus small emulator/filter/divergence arms.
/// </summary>
public sealed class Coverage100PlatformTests
{
    private static T WithWindows<T>(bool windows, Func<T> body)
    {
        var saved = OsPlatform.IsWindowsProbe;
        OsPlatform.IsWindowsProbe = () => windows;
        try
        {
            return body();
        }
        finally
        {
            OsPlatform.IsWindowsProbe = saved;
        }
    }

    [Fact]
    public void Set_unix_file_mode_is_a_no_op_where_modes_do_not_exist()
    {
        var path = Path.GetTempFileName();
        var savedApply = FileIoHelpers.ApplyUnixFileMode;
        var applied = 0;
        FileIoHelpers.ApplyUnixFileMode = (_, _) => applied++;
        try
        {
            WithWindows(true, () =>
            {
                FileIoHelpers.TrySetUnixFileMode(path, UnixFileMode.UserRead);
                return 0;
            });
            Assert.Equal(0, applied);

            WithWindows(false, () =>
            {
                FileIoHelpers.TrySetUnixFileMode(path, UnixFileMode.UserRead);
                return 0;
            });
            Assert.Equal(1, applied);
        }
        finally
        {
            FileIoHelpers.ApplyUnixFileMode = savedApply;
            File.Delete(path);
        }
    }

    [Fact]
    public void Dev_token_is_still_written_when_the_chmod_is_refused()
    {
        var path = Path.Combine(Path.GetTempPath(), "devtok-" + Guid.NewGuid().ToString("N"));
        var savedApply = FileIoHelpers.ApplyUnixFileMode;
        FileIoHelpers.ApplyUnixFileMode = (_, _) => throw new UnauthorizedAccessException("EPERM");
        try
        {
            var jwt = WithWindows(false, () => DevIdp.Setup(UtermServerConfig.Default().Auth, new DevIdp.Options { TokenPath = path }));
            Assert.Equal(jwt, File.ReadAllText(path));
        }
        finally
        {
            FileIoHelpers.ApplyUnixFileMode = savedApply;
            File.Delete(path);
        }
    }

    [Fact]
    public void Recording_sink_refuses_a_symlinked_target()
    {
        var dir = Directory.CreateTempSubdirectory("sink-");
        try
        {
            var real = Path.Combine(dir.FullName, "real.log");
            File.WriteAllText(real, "");
            var link = Path.Combine(dir.FullName, "link.log");
            try
            {
                File.CreateSymbolicLink(link, real);
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
            {
                // Windows without the symlink privilege cannot create one at all.
                Assert.True(OperatingSystem.IsWindows(), ex.ToString());
                return;
            }

            var err = Assert.Throws<IOException>(() => FileIoHelpers.SecureOpenAppend(link));
            Assert.Contains("refusing to open symlink or reparse-point recording sink", err.Message, StringComparison.Ordinal);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Raw_tail_trim_never_starts_inside_a_multibyte_character()
    {
        var emu = new TerminalEmulator(80, 24);
        // CP437 0xB0 decodes to U+2591, three UTF-8 bytes. 1400 of them are
        // 4200 bytes; the 4096-byte cut (104) lands on a continuation byte and
        // must advance to the next lead byte rather than split the character.
        var data = Enumerable.Repeat((byte)0xB0, 1400).ToArray();
        emu.Process(data);
        var tail = emu.RawTail;
        Assert.Equal(1365, tail.Length);
        Assert.All(tail, c => Assert.Equal('\u2591', c));
    }

    [Fact]
    public void Divergence_picks_a_later_output_as_majority_when_the_first_is_the_outlier()
    {
        var flags = Divergence.ComputeDivergence(new[] { "zzzzzzzz", "hello world", "hello world" }, 0.5);
        Assert.Equal(new[] { true, false, false }, flags);
    }

    [Theory]
    // The longest match QQQQ splits the strings; in the left sub-range "zy" vs
    // "z", the 'y' occurs in b only past the sub-range end, so the scan stops.
    [InlineData("zyQQQQ", "zQQQQy")]
    public void Divergence_matcher_handles_matches_beyond_the_sub_range(string a, string b)
    {
        // difflib ratio: 2*5 matched chars / 12 total = 0.8333.
        var flags = Divergence.ComputeDivergence(new[] { a, b }, 0.8);
        Assert.Equal(new[] { false, false }, flags);
        Assert.Equal(new[] { true, true }, Divergence.ComputeDivergence(new[] { a, b }, 0.9));
    }

    [Fact]
    public void Consume_iac_stops_at_eof_inside_a_subnegotiation()
    {
        using var s = new MemoryStream(new[] { InputFilters.Sb });
        InputFilters.ConsumeIac(s);
        Assert.Equal(1, s.Position);
    }
}

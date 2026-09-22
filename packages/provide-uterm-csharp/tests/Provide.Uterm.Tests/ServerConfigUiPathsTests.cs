//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// <see cref="UiPaths"/> against the reference's <c>_clean_path</c>.
/// </summary>
/// <remarks>
/// The app path is a link and redirect prefix (<c>appPath + "/operator/..."</c>),
/// so a value that begins <c>//</c> is a protocol-relative URL pointing off-site.
/// The reference collapses the whole leading run to one slash; these tests hold
/// this port to the same recorded vectors. The class name carries the
/// <c>ServerConfig</c> prefix so the Stryker test filter picks it up for
/// <c>ServerConfig/Load.cs</c>'s fallback strings.
/// </remarks>
public sealed class ServerConfigUiPathsTests
{
    private static string GoldenPath()
    {
        var parts = new[] { "packages", "provide-uterm-ts", "testdata", "serverconfig_golden.json" };
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException("serverconfig_golden.json not found above " + AppContext.BaseDirectory);
    }

    [Fact]
    public void CleanMatchesTheReferencesRecordedPaths()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(GoldenPath()));
        var failures = new List<string>();
        var count = 0;
        foreach (var record in document.RootElement.GetProperty("paths").EnumerateArray())
        {
            count++;
            var value = record.GetProperty("value").GetString();
            var fallback = record.GetProperty("fallback").GetString()!;
            var want = record.GetProperty("cleaned").GetString();
            var got = UiPaths.Clean(value, fallback);
            if (got != want)
            {
                failures.Add($"{record.GetProperty("name").GetString()}: got {got}, want {want}");
            }
        }

        Assert.True(count > 0, "the corpus recorded no paths");
        Assert.Empty(failures);
    }

    [Theory]
    [InlineData("//evil.example", "/evil.example")]
    [InlineData("///a//", "/a")]
    [InlineData("/\\evil.example", "/evil.example")]
    [InlineData("\\\\evil.example", "/evil.example")]
    [InlineData("/\t/evil.example", "/evil.example")]
    [InlineData("/\r/evil.example", "/evil.example")]
    [InlineData("/\n/evil.example", "/evil.example")]
    [InlineData("//Xapp", "/Xapp")]
    [InlineData("/a//b", "/a//b")]
    [InlineData("app/sub", "/app/sub")]
    [InlineData("  /app/  ", "/app")]
    [InlineData("///", "/")]
    [InlineData("", "/fallback")]
    [InlineData(null, "/fallback")]
    public void CleanCollapsesTheLeadingRunToOneSlash(string? value, string want)
    {
        Assert.Equal(want, UiPaths.Clean(value, "/fallback"));
    }

    [Theory]
    [InlineData(null, "/fallback")]
    [InlineData("", "/fallback")]
    [InlineData("   ", "/fallback")]
    [InlineData("/", "")]
    [InlineData("//", "")]
    [InlineData("//evil.example/", "/evil.example")]
    [InlineData("/app/", "/app")]
    [InlineData("app", "/app")]
    public void MountPrefixNeverStartsAProtocolRelativeLink(string? value, string want)
    {
        var prefix = UiPaths.MountPrefix(value, "/fallback");
        Assert.Equal(want, prefix);
        Assert.False((prefix + "/operator/s1").StartsWith("//", StringComparison.Ordinal));
    }

    [Fact]
    public void TheLoaderCleansBothUiPaths()
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-uipaths-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[ui]\napp_path = \"//evil.example/\"\nassets_path = \"/\\\\assets\"\n");
        try
        {
            var cfg = ConfigLoader.Load(path);
            Assert.Equal("/evil.example", cfg.Ui.AppPath);
            Assert.Equal("/assets", cfg.Ui.AssetsPath);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void TheLoaderFallsBackPerField()
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-uipaths-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[ui]\napp_path = \"\"\nassets_path = \"\"\n");
        try
        {
            var cfg = ConfigLoader.Load(path);
            Assert.Equal("/app", cfg.Ui.AppPath);
            Assert.Equal("/_terminal", cfg.Ui.AssetsPath);
        }
        finally
        {
            File.Delete(path);
        }
    }
}

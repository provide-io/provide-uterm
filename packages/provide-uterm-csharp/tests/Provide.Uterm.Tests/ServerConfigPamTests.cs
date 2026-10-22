//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// <c>[pam] capture_socket_mode</c> loads as the reference loads it: this port
/// binds no capture socket, but it accepts a config that names 0o600 or 0o660
/// and refuses any other mode, or a value that is not an integer, with the
/// reference's own text.
/// </summary>
public sealed class ServerConfigPamTests
{
    private static UtermServerConfig Load(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-pam-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[pam]\n" + body + "\n");
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Theory]
    [InlineData("capture_socket_mode = 0o600")]
    [InlineData("capture_socket_mode = 0o660")]
    [InlineData("capture_socket_mode = 432")]
    [InlineData("mode = \"capture\"")]
    public void OwnerOnlyAndOwnerAndGroupAreAccepted(string body)
    {
        Assert.NotNull(Load(body));
    }

    [Theory]
    [InlineData("0o666", "0o666")]
    [InlineData("0o664", "0o664")]
    [InlineData("0o640", "0o640")]
    [InlineData("0o700", "0o700")]
    [InlineData("0o770", "0o770")]
    [InlineData("0o2660", "0o2660")]
    [InlineData("0", "0o0")]
    [InlineData("-1", "-0o1")]
    [InlineData("-384", "-0o600")]
    [InlineData("-9223372036854775808", "-0o1000000000000000000000")]
    [InlineData("9223372036854775807", "0o777777777777777777777")]
    public void AnyOtherModeIsRefusedQuotingItAsPythonsOctDoes(string written, string shown)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("capture_socket_mode = " + written));
        Assert.Equal("pam.capture_socket_mode must be 0o600 or 0o660, got: " + shown, error.Message);
    }

    [Theory]
    [InlineData("\"0o660\"")]
    [InlineData("\"432\"")]
    [InlineData("432.0")]
    [InlineData("true")]
    [InlineData("[432]")]
    public void AModeThatIsNotAnIntegerIsRefused(string written)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("capture_socket_mode = " + written));
        Assert.Equal("pam.capture_socket_mode must be an integer: 0o600 or 0o660", error.Message);
    }

    private static string CorpusPath()
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

    /// <summary>
    /// The <c>capture_socket_mode</c> cases of the shared corpus
    /// (<c>gen_serverconfig_golden.py</c>) are accepted or refused with the
    /// reference's text. The corpus's other PamConfig cases are the relay URL
    /// rule, which this port, carrying no PAM model, does not check.
    /// </summary>
    [Fact]
    public void TheCaptureSocketModeMatchesTheReferenceCorpus()
    {
        using var document = JsonDocument.Parse(File.ReadAllText(CorpusPath()));
        var failures = new List<string>();
        var count = 0;
        foreach (var record in document.RootElement.GetProperty("models").EnumerateArray())
        {
            if (record.GetProperty("model").GetString() != "PamConfig"
                || !record.GetProperty("kwargs").TryGetProperty("capture_socket_mode", out var value))
            {
                continue;
            }

            count++;
            var want = record.GetProperty("error").ValueKind == JsonValueKind.Null
                ? null
                : record.GetProperty("error").GetString();
            string? got = null;
            try
            {
                // The recorded JSON is already a TOML literal, as the reference
                // wrote it: 432.0 stays a float and 432 an integer, a number list
                // is an array, and a plain recorded string a basic string.
                Load("capture_socket_mode = " + value.GetRawText());
            }
            catch (ArgumentException error)
            {
                got = error.Message;
            }

            if (got != want)
            {
                failures.Add($"{record.GetProperty("name").GetString()}: got {got}, want {want}");
            }
        }

        Assert.True(count > 0, "the corpus recorded no capture_socket_mode cases");
        Assert.Empty(failures);
    }
}

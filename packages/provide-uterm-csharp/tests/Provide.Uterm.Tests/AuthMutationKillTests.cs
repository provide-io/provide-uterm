//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Auth;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>Kill boolean-op mutants in Auth.cs authorized_keys / option parsing.</summary>
public class AuthMutationKillTests
{
    private static string FakeKeyLine(string? options = null)
    {
        // Valid-looking base64 payload (need enough length for fingerprint path).
        var b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(new string('A', 32)));
        var key = $"ssh-ed25519 {b64} bob@host";
        return options is null ? key : $"{options} {key}";
    }

    [Fact]
    public async Task AuthorizedKeys_SkipsCommentAndEmpty_FindsKey()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-auth-" + Guid.NewGuid().ToString("n"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "authorized_keys");
            var line = FakeKeyLine();
            // Comment + blank + key: || vs && on skip condition must still skip comments.
            File.WriteAllText(path, "# comment only\n\n" + line + "\n");
            var fp = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes(line));
            var resolver = new SshAuth.AuthorizedKeysFileResolver(path);
            var id = await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "bob");
            Assert.NotNull(id);

            // File with ONLY a comment must not resolve any fingerprint.
            File.WriteAllText(path, "# only comment\n");
            Assert.Null(await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "bob"));
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public async Task AuthorizedKeys_OptionsWithQuotedComma_StillResolves()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-auth-" + Guid.NewGuid().ToString("n"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "authorized_keys");
            // Comma inside quotes must not split options (ch == ',' && !inQuotes).
            var line = FakeKeyLine("command=\"echo,hi\",no-port-forwarding");
            File.WriteAllText(path, line + "\n");
            var keyPart = line[(line.LastIndexOf("ssh-ed25519", StringComparison.Ordinal))..];
            var fp = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes(keyPart));
            var resolver = new SshAuth.AuthorizedKeysFileResolver(path);
            var id = await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "bob");
            Assert.NotNull(id);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public async Task AuthorizedKeys_OptionsWhitespaceInQuotes_StillResolves()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-auth-" + Guid.NewGuid().ToString("n"));
        Directory.CreateDirectory(dir);
        try
        {
            var path = Path.Combine(dir, "authorized_keys");
            // Whitespace inside quotes must not end first token (IsWhiteSpace && !inQuotes).
            var line = FakeKeyLine("from=\"a b\"");
            File.WriteAllText(path, line + "\n");
            var keyPart = line[(line.LastIndexOf("ssh-ed25519", StringComparison.Ordinal))..];
            var fp = SshAuth.FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes(keyPart));
            var resolver = new SshAuth.AuthorizedKeysFileResolver(path);
            var id = await resolver.ResolveAsync(fp, Encoding.UTF8.GetBytes(line), "bob");
            Assert.NotNull(id);
        }
        finally
        {
            Directory.Delete(dir, recursive: true);
        }
    }
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;

namespace Provide.Uterm.Auth;

/// <summary>
/// Identity successfully resolved from an SSH public key.
/// Port of provide.uterm.auth / packages/provide-uterm-go/auth.
/// </summary>
public sealed class ResolvedIdentity
{
    public required string Subject { get; init; }
    public Dictionary<string, object?> Claims { get; init; } = new();
    public string Fingerprint { get; init; } = "";
}

/// <summary>Maps an SSH public key to an application identity.</summary>
public interface ISshKeyResolver
{
    Task<ResolvedIdentity?> ResolveAsync(
        string fingerprint,
        byte[] pubkeyBlob,
        string username,
        CancellationToken cancellationToken = default);
}

/// <summary>Never resolves anything.</summary>
public sealed class NullResolver : ISshKeyResolver
{
    public Task<ResolvedIdentity?> ResolveAsync(
        string fingerprint,
        byte[] pubkeyBlob,
        string username,
        CancellationToken cancellationToken = default) =>
        Task.FromResult<ResolvedIdentity?>(null);
}

/// <summary>SSH-key fingerprint and authorized_keys helpers.</summary>
public static class SshAuth
{
    private static readonly string[] TextKeyPrefixes = ["ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"];

    /// <summary>
    /// Compute an OpenSSH-style SHA256 fingerprint from raw key bytes.
    /// Returns a string like "SHA256:…".
    /// </summary>
    public static string FingerprintFromOpenSshBlob(byte[] blob)
    {
        var binary = CoerceToBinaryPubkey(blob);
        var digest = SHA256.HashData(binary);
        var b64 = Convert.ToBase64String(digest).TrimEnd('=');
        return "SHA256:" + b64;
    }

    private static bool HasKeytypePrefix(string s) =>
        TextKeyPrefixes.Any(p => s.StartsWith(p, StringComparison.Ordinal));

    private static byte[] CoerceToBinaryPubkey(byte[] blob)
    {
        var stripped = Encoding.UTF8.GetString(blob).Trim();
        if (HasKeytypePrefix(stripped))
        {
            var parts = stripped.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length < 2)
            {
                throw new FormatException("malformed OpenSSH public key line");
            }

            return Convert.FromBase64String(parts[1]);
        }

        return Encoding.UTF8.GetBytes(stripped);
    }

    /// <summary>Resolver over an OpenSSH authorized_keys file.</summary>
    public sealed class AuthorizedKeysFileResolver : ISshKeyResolver
    {
        private readonly string _path;

        public AuthorizedKeysFileResolver(string path) => _path = path;

        public Task<ResolvedIdentity?> ResolveAsync(
            string fingerprint,
            byte[] pubkeyBlob,
            string username,
            CancellationToken cancellationToken = default)
        {
            foreach (var entry in LoadEntries())
            {
                if (entry.Fingerprint == fingerprint)
                {
                    return Task.FromResult<ResolvedIdentity?>(new ResolvedIdentity
                    {
                        Subject = entry.Subject,
                        Claims = entry.Claims,
                        Fingerprint = fingerprint,
                    });
                }
            }

            return Task.FromResult<ResolvedIdentity?>(null);
        }

        private IEnumerable<AuthorizedKeyEntry> LoadEntries()
        {
            if (!File.Exists(_path))
            {
                yield break;
            }

            foreach (var rawLine in File.ReadAllLines(_path))
            {
                var line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith('#'))
                {
                    continue;
                }

                AuthorizedKeyEntry entry;
                try
                {
                    entry = ParseAuthorizedKeysLine(line);
                }
                catch
                {
                    continue;
                }

                yield return entry;
            }
        }
    }

    private sealed class AuthorizedKeyEntry
    {
        public required string Fingerprint { get; init; }
        public required string Subject { get; init; }
        public required Dictionary<string, object?> Claims { get; init; }
    }

    private static AuthorizedKeyEntry ParseAuthorizedKeysLine(string line)
    {
        var firstTokenEnd = FindFirstTokenEnd(line);
        var firstToken = line[..firstTokenEnd];

        string optionsStr;
        string rest;
        if (HasKeytypePrefix(firstToken))
        {
            optionsStr = "";
            rest = line;
        }
        else
        {
            optionsStr = firstToken;
            rest = line[firstTokenEnd..].TrimStart();
        }

        var fields = rest.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (fields.Length < 2)
        {
            throw new FormatException("missing key payload");
        }

        var keytype = fields[0];
        var payload = fields[1];
        var comment = "";
        if (fields.Length > 2)
        {
            var idx = rest.IndexOf(payload, StringComparison.Ordinal) + payload.Length;
            comment = rest[idx..].Trim();
        }

        var fp = FingerprintFromOpenSshBlob(Encoding.UTF8.GetBytes(keytype + " " + payload));
        var opts = optionsStr.Length > 0 ? ParseOptions(optionsStr) : new Dictionary<string, object?>();

        var subject = "";
        if (opts.TryGetValue("subject", out var subjObj) && subjObj is string s && s.Length > 0)
        {
            subject = s;
        }

        opts.Remove("subject");
        if (subject.Length == 0)
        {
            subject = comment;
        }

        if (subject.Length == 0)
        {
            subject = "key:" + fp;
        }

        var claims = new Dictionary<string, object?>();
        var leftover = new Dictionary<string, object?>();
        foreach (var (key, value) in opts)
        {
            if (key.StartsWith("claim-", StringComparison.Ordinal))
            {
                claims[key["claim-".Length..]] = value;
            }
            else
            {
                leftover[key] = value;
            }
        }

        if (leftover.Count > 0)
        {
            claims["_options"] = leftover;
        }

        return new AuthorizedKeyEntry { Fingerprint = fp, Subject = subject, Claims = claims };
    }

    private static int FindFirstTokenEnd(string line)
    {
        var inQuotes = false;
        for (var i = 0; i < line.Length; i++)
        {
            var ch = line[i];
            if (ch == '"')
            {
                inQuotes = !inQuotes;
            }
            else if (char.IsWhiteSpace(ch) && !inQuotes)
            {
                return i;
            }
        }

        return line.Length;
    }

    private static Dictionary<string, object?> ParseOptions(string optionsStr)
    {
        var outMap = new Dictionary<string, object?>();
        foreach (var token in SplitOptions(optionsStr))
        {
            var eq = token.IndexOf('=');
            if (eq >= 0)
            {
                var key = token[..eq].Trim();
                var value = token[(eq + 1)..].Trim().Trim('"');
                outMap[key] = value;
            }
            else
            {
                outMap[token.Trim()] = true;
            }
        }

        return outMap;
    }

    private static List<string> SplitOptions(string optionsStr)
    {
        var outList = new List<string>();
        var buf = new StringBuilder();
        var inQuotes = false;
        foreach (var ch in optionsStr)
        {
            if (ch == '"')
            {
                inQuotes = !inQuotes;
                buf.Append(ch);
            }
            else if (ch == ',' && !inQuotes)
            {
                if (buf.Length > 0)
                {
                    outList.Add(buf.ToString());
                    buf.Clear();
                }
            }
            else
            {
                buf.Append(ch);
            }
        }

        if (buf.Length > 0)
        {
            outList.Add(buf.ToString());
        }

        return outList;
    }
}

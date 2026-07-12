//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Transports;

/// <summary>
/// Minimal OpenSSH known_hosts matcher for SSH client host-key verification.
/// Supports plain host/IP entries (comma-separated aliases) and optional [host]:port form.
/// Hashed hostnames (|1|…) are not matched (fail closed unless another plain entry matches).
/// </summary>
public static class KnownHosts
{
    /// <summary>
    /// Returns true when any known_hosts file contains a matching host key for host:port.
    /// </summary>
    public static bool Matches(
        string host,
        int port,
        string hostKeyName,
        byte[] hostKey,
        IReadOnlyList<string> knownHostsFiles)
    {
        if (string.IsNullOrEmpty(host) || hostKey is null || hostKey.Length == 0 ||
            knownHostsFiles is null || knownHostsFiles.Count == 0)
        {
            return false;
        }

        var keyB64 = Convert.ToBase64String(hostKey);
        var candidates = HostCandidates(host, port);
        foreach (var path in knownHostsFiles)
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            {
                continue;
            }

            foreach (var raw in File.ReadLines(path))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line.StartsWith('#'))
                {
                    continue;
                }

                // Strip optional markers (@cert-authority, @revoked)
                if (line.StartsWith('@'))
                {
                    var sp = line.IndexOf(' ');
                    if (sp < 0)
                    {
                        continue;
                    }

                    line = line[(sp + 1)..].TrimStart();
                }

                var parts = SplitWs(line);
                if (parts.Count < 3)
                {
                    continue;
                }

                var hostsField = parts[0];
                var keyType = parts[1];
                var keyData = parts[2];
                if (!string.Equals(keyType, hostKeyName, StringComparison.Ordinal) &&
                    !KeyTypeAliasesMatch(keyType, hostKeyName))
                {
                    continue;
                }

                if (!string.Equals(keyData, keyB64, StringComparison.Ordinal))
                {
                    continue;
                }

                // Hashed hostnames: skip (cannot verify without salt recomputation path here).
                if (hostsField.StartsWith("|1|", StringComparison.Ordinal))
                {
                    continue;
                }

                foreach (var alias in hostsField.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                {
                    if (candidates.Contains(alias))
                    {
                        return true;
                    }
                }
            }
        }

        return false;
    }

    /// <summary>Load first existing known_hosts path list for diagnostics (existence only).</summary>
    public static IReadOnlyList<string> ExistingFiles(IReadOnlyList<string> paths) =>
        paths.Where(p => !string.IsNullOrWhiteSpace(p) && File.Exists(p)).ToList();

    internal static HashSet<string> HostCandidates(string host, int port)
    {
        var set = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { host };
        if (port > 0 && port != 22)
        {
            set.Add($"[{host}]:{port}");
            set.Add($"{host}:{port}");
        }
        else
        {
            set.Add($"[{host}]:22");
        }

        return set;
    }

    private static bool KeyTypeAliasesMatch(string a, string b) =>
        string.Equals(NormalizeKeyType(a), NormalizeKeyType(b), StringComparison.Ordinal);

    private static string NormalizeKeyType(string t) => t switch
    {
        "ssh-rsa" => "ssh-rsa",
        "rsa-sha2-256" => "ssh-rsa",
        "rsa-sha2-512" => "ssh-rsa",
        _ => t,
    };

    private static List<string> SplitWs(string line)
    {
        var parts = new List<string>(4);
        var sb = new StringBuilder();
        foreach (var ch in line)
        {
            if (char.IsWhiteSpace(ch))
            {
                if (sb.Length > 0)
                {
                    parts.Add(sb.ToString());
                    sb.Clear();
                }
            }
            else
            {
                sb.Append(ch);
            }
        }

        if (sb.Length > 0)
        {
            parts.Add(sb.ToString());
        }

        return parts;
    }
}

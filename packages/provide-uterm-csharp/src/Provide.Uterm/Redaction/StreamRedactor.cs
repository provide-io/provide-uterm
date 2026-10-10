//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Redaction;

/// <summary>
/// A regex-based redaction rule. Port of the reference's
/// <c>provide.uterm.server.bridge.hub.ext.RedactionRule</c>.
/// </summary>
public sealed record RedactionRule(string Pattern, string Replacement = "[REDACTED]");

/// <summary>
/// Rule-driven redactor: every rule is folded into one alternation and each
/// match is replaced by the replacement of the rule that matched it. Port of
/// the reference's <c>StreamRedactor</c>
/// (<c>provide-uterm-server/.../bridge/hub/redaction.py</c>).
///
/// <para>The reference finds the owning rule from <c>match.lastindex</c> and the
/// rules' group start indices; here each rule is wrapped in its own named group,
/// which identifies the rule directly and is not shifted by a rule's own
/// capturing groups. Either way the alternation is tried left to right, so the
/// first rule that matches at a position wins, as in the reference.</para>
/// </summary>
public sealed class StreamRedactor
{
    private const string GroupPrefix = "__uterm_rule";

    private readonly Regex? _pattern;
    private readonly List<string> _replacements = [];

    /// <summary>
    /// Build from <paramref name="rules"/>. A rule whose pattern does not
    /// compile is skipped, as the reference skips one <c>re</c> rejects.
    /// </summary>
    public StreamRedactor(IEnumerable<RedactionRule>? rules)
    {
        var combined = new StringBuilder();
        foreach (var rule in rules ?? [])
        {
            try
            {
                _ = new Regex(rule.Pattern);
            }
            catch (ArgumentException)
            {
                continue;
            }

            if (combined.Length > 0) combined.Append('|');
            combined.Append("(?<").Append(GroupPrefix).Append(_replacements.Count).Append('>')
                .Append(rule.Pattern).Append(')');
            _replacements.Add(rule.Replacement);
        }

        if (_replacements.Count > 0)
        {
            _pattern = new Regex(combined.ToString(), RegexOptions.Compiled);
        }
    }

    /// <summary>Apply every rule to <paramref name="data"/> in a single pass.</summary>
    public string Redact(string data) =>
        _pattern is null ? data : _pattern.Replace(data, ReplacementFor);

    private string ReplacementFor(Match match)
    {
        var index = 0;
        while (!match.Groups[GroupPrefix + index].Success)
        {
            index++;
        }

        return _replacements[index];
    }
}

/// <summary>
/// The default recording-redaction rules. Port of the reference's
/// <c>default_rules()</c> (<c>.../bridge/hub/redaction_defaults.py</c>): the
/// set a hosted session's recording runs through when
/// <c>recording.redact_sensitive</c> is on (the default), via
/// <c>_build_recording_redactor</c> in <c>server/runtime_helpers.py</c>.
/// Patterns are copied verbatim; see the reference for each one's rationale.
/// </summary>
public static class RedactionDefaults
{
    /// <summary>Stable order: anchored credential formats first, generic shapes last.</summary>
    public static IReadOnlyList<RedactionRule> DefaultRules() =>
    [
        new(@"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b", "[AWS_ACCESS_KEY_REDACTED]"),
        new(@"(?i:aws[_ -]?secret[_ -]?access[_ -]?key\s*[:=]\s*['\""]?[A-Za-z0-9/+=]{40}['\""]?)",
            "[AWS_SECRET_REDACTED]"),
        new(@"\bgh[opusr]_[A-Za-z0-9_]{36,251}\b", "[GITHUB_TOKEN_REDACTED]"),
        new(@"\bxox[abeprs]-(?:[0-9]+-){2,}[A-Za-z0-9-]{20,}\b", "[SLACK_TOKEN_REDACTED]"),
        new(@"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[JWT_REDACTED]"),
        new(
            @"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
            + @"[\s\S]+?"
            + @"-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
            "[PRIVATE_KEY_REDACTED]"),
        new(@"(?i:\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-+/=]+))", "Authorization: Bearer [REDACTED]"),
        new(@"(?i:\b(?:password|passwd|pwd)\s*[:=]\s*['\""]?(\S{1,128}?)['\""]?(?=\s|$|,|;|&))", "[PASSWORD_REDACTED]"),
        new(@"(?i:\bapi[_-]?key\s*[:=]\s*['\""]?(\S{6,128}?)['\""]?(?=\s|$|,|;|&))", "[API_KEY_REDACTED]"),
        new(@"(?i:\btoken\s*[:=]\s*['\""]?(\S{8,256}?)['\""]?(?=\s|$|,|;|&))", "[TOKEN_REDACTED]"),
    ];
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Annotation;

/// <summary>
/// The built-in detection rules. Port of the Python module
/// <c>provide.uterm.annotation._rules</c>, pinned by
/// <c>tests/Provide.Uterm.Tests/testdata/annotation_golden.json</c>.
///
/// Ordered most-specific first <em>within</em> each category, because only the
/// first rule to match a category produces an annotation: without that ordering
/// a line mentioning a password would be reported by the generic rule rather
/// than the one that names what it actually found.
/// </summary>
internal static class BuiltinDetectionRules
{
    /// <summary>Every built-in rule applies to both directions of a session.</summary>
    private static readonly string[] Both = ["read", "send"];

    internal static readonly IReadOnlyList<DetectionRule> All =
    [
        // credentials
        Rule("cred.aws_access_key", "credential_exposure", @"AKIA[0-9A-Z]{12}", "high",
            "AWS access key detected in {event_type}", "credentials"),
        Rule("cred.github_token", "credential_exposure", @"gh[psourx]_[A-Za-z0-9_]{8}", "high",
            "GitHub token detected in {event_type}", "credentials"),
        Rule("cred.generic_secret", "credential_exposure", @"(?i)(password|secret|token|api_key)\s*[=:]", "high",
            "Secret assignment detected in {event_type}", "credentials"),
        Rule("cred.bearer_token", "credential_exposure", @"Bearer\s+\S{8}", "high",
            "Bearer token detected in {event_type}", "credentials"),
        Rule("cred.private_key_header", "credential_exposure", @"-----BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY-----", "high",
            "Private key header detected: {match}", "credentials"),
        // escalation
        Rule("esc.sudo", "privilege_escalation", @"\bsudo\b", "high",
            "sudo command detected: {match}", "escalation"),
        Rule("esc.su_dash", "privilege_escalation", @"\bsu\s+-", "high",
            "su - (switch to root) detected: {match}", "escalation"),
        Rule("esc.pkexec", "privilege_escalation", @"\bpkexec\b", "high",
            "pkexec privilege escalation detected: {match}", "escalation"),
        // destructive
        Rule("dest.rm_rf", "destructive_command", @"\brm\s+(-[rRf]{2,}|-[rR]\s+-f|-f\s+-[rR])", "critical",
            "Recursive force-remove detected: {match}", "destructive"),
        Rule("dest.drop_table", "destructive_command", @"(?i)\bDROP\s+(TABLE|DATABASE)\b", "critical",
            "SQL DROP statement detected: {match}", "destructive"),
        Rule("dest.kubectl_delete", "destructive_command", @"\bkubectl\s+delete\b", "critical",
            "kubectl delete command detected: {match}", "destructive"),
        Rule("dest.dd_if", "destructive_command", @"\bdd\s+if=", "critical",
            "dd disk-copy command detected: {match}", "destructive"),
        Rule("dest.mkfs", "destructive_command", @"\bmkfs\.", "critical",
            "mkfs (format filesystem) detected: {match}", "destructive"),
        // connections
        Rule("conn.ssh", "outbound_connection", @"\bssh\s+[\w.\-]+@", "info",
            "SSH connection detected: {match}", "connections"),
        Rule("conn.curl", "outbound_connection", @"\bcurl\b.*https?://", "info",
            "curl HTTP request detected: {match}", "connections"),
        Rule("conn.wget", "outbound_connection", @"\bwget\b.*https?://", "info",
            "wget HTTP request detected: {match}", "connections"),
        Rule("conn.scp", "outbound_connection", @"\bscp\b", "info",
            "scp file transfer detected: {match}", "connections"),
        // lifecycle
        Rule("life.exit", "session_lifecycle", @"\bexit\b", "info",
            "exit command detected: {match}", "lifecycle"),
        Rule("life.shutdown", "session_lifecycle", @"\bshutdown\b", "info",
            "shutdown command detected: {match}", "lifecycle"),
        Rule("life.reboot", "session_lifecycle", @"\breboot\b", "info",
            "reboot command detected: {match}", "lifecycle"),
    ];

    private static DetectionRule Rule(
        string ruleId, string label, string pattern, string severity, string template, string category) => new()
    {
        RuleId = ruleId,
        Label = label,
        Pattern = new Regex(pattern, RegexOptions.Compiled | RegexOptions.CultureInvariant),
        Severity = severity,
        DescriptionTemplate = template,
        EventTypes = new HashSet<string>(Both, StringComparer.Ordinal),
        Category = category,
    };
}

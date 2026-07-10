//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

// bothEventTypes is shared by every built-in rule: all apply to both inbound
// ("read") and outbound ("send") event streams.
var bothEventTypes = NewEventTypeSet("read", "send")

// BuiltinRules is the built-in detection rule set, ordered most-specific first
// within each category. It mirrors the Python BUILTIN_RULES list exactly
// (order, ids, patterns, severities, templates, categories).
var BuiltinRules = []DetectionRule{
	// -----------------------------------------------------------------------
	// Credentials — high severity
	// -----------------------------------------------------------------------
	{
		RuleID:              "cred.aws_access_key",
		Label:               "credential_exposure",
		Pattern:             mustCompile(`AKIA[0-9A-Z]{12}`),
		Severity:            "high",
		DescriptionTemplate: "AWS access key detected in {event_type}",
		EventTypes:          bothEventTypes,
		Category:            "credentials",
	},
	{
		RuleID:              "cred.github_token",
		Label:               "credential_exposure",
		Pattern:             mustCompile(`gh[psourx]_[A-Za-z0-9_]{8}`),
		Severity:            "high",
		DescriptionTemplate: "GitHub token detected in {event_type}",
		EventTypes:          bothEventTypes,
		Category:            "credentials",
	},
	{
		RuleID:              "cred.generic_secret",
		Label:               "credential_exposure",
		Pattern:             mustCompile(`(?i)(password|secret|token|api_key)\s*[=:]`),
		Severity:            "high",
		DescriptionTemplate: "Secret assignment detected in {event_type}",
		EventTypes:          bothEventTypes,
		Category:            "credentials",
	},
	{
		RuleID:              "cred.bearer_token",
		Label:               "credential_exposure",
		Pattern:             mustCompile(`Bearer\s+\S{8}`),
		Severity:            "high",
		DescriptionTemplate: "Bearer token detected in {event_type}",
		EventTypes:          bothEventTypes,
		Category:            "credentials",
	},
	{
		RuleID:              "cred.private_key_header",
		Label:               "credential_exposure",
		Pattern:             mustCompile(`-----BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY-----`),
		Severity:            "high",
		DescriptionTemplate: "Private key header detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "credentials",
	},
	// -----------------------------------------------------------------------
	// Privilege escalation — high severity
	// -----------------------------------------------------------------------
	{
		RuleID:              "esc.sudo",
		Label:               "privilege_escalation",
		Pattern:             mustCompile(`\bsudo\b`),
		Severity:            "high",
		DescriptionTemplate: "sudo command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "escalation",
	},
	{
		RuleID:              "esc.su_dash",
		Label:               "privilege_escalation",
		Pattern:             mustCompile(`\bsu\s+-`),
		Severity:            "high",
		DescriptionTemplate: "su - (switch to root) detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "escalation",
	},
	{
		RuleID:              "esc.pkexec",
		Label:               "privilege_escalation",
		Pattern:             mustCompile(`\bpkexec\b`),
		Severity:            "high",
		DescriptionTemplate: "pkexec privilege escalation detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "escalation",
	},
	// -----------------------------------------------------------------------
	// Destructive commands — critical severity
	// -----------------------------------------------------------------------
	{
		RuleID:              "dest.rm_rf",
		Label:               "destructive_command",
		Pattern:             mustCompile(`\brm\s+(-[rRf]{2,}|-[rR]\s+-f|-f\s+-[rR])`),
		Severity:            "critical",
		DescriptionTemplate: "Recursive force-remove detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "destructive",
	},
	{
		RuleID:              "dest.drop_table",
		Label:               "destructive_command",
		Pattern:             mustCompile(`(?i)\bDROP\s+(TABLE|DATABASE)\b`),
		Severity:            "critical",
		DescriptionTemplate: "SQL DROP statement detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "destructive",
	},
	{
		RuleID:              "dest.kubectl_delete",
		Label:               "destructive_command",
		Pattern:             mustCompile(`\bkubectl\s+delete\b`),
		Severity:            "critical",
		DescriptionTemplate: "kubectl delete command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "destructive",
	},
	{
		RuleID:              "dest.dd_if",
		Label:               "destructive_command",
		Pattern:             mustCompile(`\bdd\s+if=`),
		Severity:            "critical",
		DescriptionTemplate: "dd disk-copy command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "destructive",
	},
	{
		RuleID:              "dest.mkfs",
		Label:               "destructive_command",
		Pattern:             mustCompile(`\bmkfs\.`),
		Severity:            "critical",
		DescriptionTemplate: "mkfs (format filesystem) detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "destructive",
	},
	// -----------------------------------------------------------------------
	// Outbound connections — info severity
	// -----------------------------------------------------------------------
	{
		RuleID:              "conn.ssh",
		Label:               "outbound_connection",
		Pattern:             mustCompile(`\bssh\s+[\w.\-]+@`),
		Severity:            "info",
		DescriptionTemplate: "SSH connection detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "connections",
	},
	{
		RuleID:              "conn.curl",
		Label:               "outbound_connection",
		Pattern:             mustCompile(`\bcurl\b.*https?://`),
		Severity:            "info",
		DescriptionTemplate: "curl HTTP request detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "connections",
	},
	{
		RuleID:              "conn.wget",
		Label:               "outbound_connection",
		Pattern:             mustCompile(`\bwget\b.*https?://`),
		Severity:            "info",
		DescriptionTemplate: "wget HTTP request detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "connections",
	},
	{
		RuleID:              "conn.scp",
		Label:               "outbound_connection",
		Pattern:             mustCompile(`\bscp\b`),
		Severity:            "info",
		DescriptionTemplate: "scp file transfer detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "connections",
	},
	// -----------------------------------------------------------------------
	// Session lifecycle — info severity
	// -----------------------------------------------------------------------
	{
		RuleID:              "life.exit",
		Label:               "session_lifecycle",
		Pattern:             mustCompile(`\bexit\b`),
		Severity:            "info",
		DescriptionTemplate: "exit command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "lifecycle",
	},
	{
		RuleID:              "life.shutdown",
		Label:               "session_lifecycle",
		Pattern:             mustCompile(`\bshutdown\b`),
		Severity:            "info",
		DescriptionTemplate: "shutdown command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "lifecycle",
	},
	{
		RuleID:              "life.reboot",
		Label:               "session_lifecycle",
		Pattern:             mustCompile(`\breboot\b`),
		Severity:            "info",
		DescriptionTemplate: "reboot command detected: {match}",
		EventTypes:          bothEventTypes,
		Category:            "lifecycle",
	},
}

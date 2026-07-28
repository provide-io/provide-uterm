//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The built-in detection rules.
 *
 * Port of the Python module `provide.uterm.annotation._rules`.
 *
 * Ordered most-specific first *within* each category, because only the first
 * rule to match a category produces an annotation: without that ordering a
 * line mentioning a password would be reported by the generic rule rather
 * than the one that names what it actually found.
 */

import type { DetectionRule } from "./models.ts";

/** Every rule the detector ships with. */
export const BUILTIN_RULES: readonly DetectionRule[] = [
  {
    ruleId: "cred.aws_access_key",
    label: "credential_exposure",
    pattern: /AKIA[0-9A-Z]{12}/,
    severity: "high",
    descriptionTemplate: "AWS access key detected in {event_type}",
    eventTypes: new Set(["read", "send"]),
    category: "credentials",
  },
  {
    ruleId: "cred.github_token",
    label: "credential_exposure",
    pattern: /gh[psourx]_[A-Za-z0-9_]{8}/,
    severity: "high",
    descriptionTemplate: "GitHub token detected in {event_type}",
    eventTypes: new Set(["read", "send"]),
    category: "credentials",
  },
  {
    ruleId: "cred.generic_secret",
    label: "credential_exposure",
    pattern: /(password|secret|token|api_key)\s*[=:]/i,
    severity: "high",
    descriptionTemplate: "Secret assignment detected in {event_type}",
    eventTypes: new Set(["read", "send"]),
    category: "credentials",
  },
  {
    ruleId: "cred.bearer_token",
    label: "credential_exposure",
    pattern: /Bearer\s+\S{8}/,
    severity: "high",
    descriptionTemplate: "Bearer token detected in {event_type}",
    eventTypes: new Set(["read", "send"]),
    category: "credentials",
  },
  {
    ruleId: "cred.private_key_header",
    label: "credential_exposure",
    pattern: /-----BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY-----/,
    severity: "high",
    descriptionTemplate: "Private key header detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "credentials",
  },
  {
    ruleId: "esc.sudo",
    label: "privilege_escalation",
    pattern: /\bsudo\b/,
    severity: "high",
    descriptionTemplate: "sudo command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "escalation",
  },
  {
    ruleId: "esc.su_dash",
    label: "privilege_escalation",
    pattern: /\bsu\s+-/,
    severity: "high",
    descriptionTemplate: "su - (switch to root) detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "escalation",
  },
  {
    ruleId: "esc.pkexec",
    label: "privilege_escalation",
    pattern: /\bpkexec\b/,
    severity: "high",
    descriptionTemplate: "pkexec privilege escalation detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "escalation",
  },
  {
    ruleId: "dest.rm_rf",
    label: "destructive_command",
    pattern: /\brm\s+(-[rRf]{2,}|-[rR]\s+-f|-f\s+-[rR])/,
    severity: "critical",
    descriptionTemplate: "Recursive force-remove detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "destructive",
  },
  {
    ruleId: "dest.drop_table",
    label: "destructive_command",
    pattern: /\bDROP\s+(TABLE|DATABASE)\b/i,
    severity: "critical",
    descriptionTemplate: "SQL DROP statement detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "destructive",
  },
  {
    ruleId: "dest.kubectl_delete",
    label: "destructive_command",
    pattern: /\bkubectl\s+delete\b/,
    severity: "critical",
    descriptionTemplate: "kubectl delete command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "destructive",
  },
  {
    ruleId: "dest.dd_if",
    label: "destructive_command",
    pattern: /\bdd\s+if=/,
    severity: "critical",
    descriptionTemplate: "dd disk-copy command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "destructive",
  },
  {
    ruleId: "dest.mkfs",
    label: "destructive_command",
    pattern: /\bmkfs\./,
    severity: "critical",
    descriptionTemplate: "mkfs (format filesystem) detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "destructive",
  },
  {
    ruleId: "conn.ssh",
    label: "outbound_connection",
    pattern: /\bssh\s+[\w.-]+@/,
    severity: "info",
    descriptionTemplate: "SSH connection detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "connections",
  },
  {
    ruleId: "conn.curl",
    label: "outbound_connection",
    pattern: /\bcurl\b.*https?:\/\//,
    severity: "info",
    descriptionTemplate: "curl HTTP request detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "connections",
  },
  {
    ruleId: "conn.wget",
    label: "outbound_connection",
    pattern: /\bwget\b.*https?:\/\//,
    severity: "info",
    descriptionTemplate: "wget HTTP request detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "connections",
  },
  {
    ruleId: "conn.scp",
    label: "outbound_connection",
    pattern: /\bscp\b/,
    severity: "info",
    descriptionTemplate: "scp file transfer detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "connections",
  },
  {
    ruleId: "life.exit",
    label: "session_lifecycle",
    pattern: /\bexit\b/,
    severity: "info",
    descriptionTemplate: "exit command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "lifecycle",
  },
  {
    ruleId: "life.shutdown",
    label: "session_lifecycle",
    pattern: /\bshutdown\b/,
    severity: "info",
    descriptionTemplate: "shutdown command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "lifecycle",
  },
  {
    ruleId: "life.reboot",
    label: "session_lifecycle",
    pattern: /\breboot\b/,
    severity: "info",
    descriptionTemplate: "reboot command detected: {match}",
    eventTypes: new Set(["read", "send"]),
    category: "lifecycle",
  },
];

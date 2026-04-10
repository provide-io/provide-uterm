#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Built-in detection rules for session annotation."""

from __future__ import annotations

import re

from provide.terminal.bridge.annotation._models import DetectionRule

# All built-in rules apply to both inbound and outbound event streams.
_BOTH: frozenset[str] = frozenset({"read", "send"})

# ---------------------------------------------------------------------------
# Built-in rules — ordered most-specific first within each category.
# ---------------------------------------------------------------------------

BUILTIN_RULES: list[DetectionRule] = [
    # -------------------------------------------------------------------------
    # Credentials — high severity
    # -------------------------------------------------------------------------
    DetectionRule(
        rule_id="cred.aws_access_key",
        label="credential_exposure",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        severity="high",
        description_template="AWS access key detected: {match}",
        event_types=_BOTH,
        category="credentials",
    ),
    DetectionRule(
        rule_id="cred.github_token",
        label="credential_exposure",
        pattern=re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}"),
        severity="high",
        description_template="GitHub token detected: {match}",
        event_types=_BOTH,
        category="credentials",
    ),
    DetectionRule(
        rule_id="cred.generic_secret",
        label="credential_exposure",
        pattern=re.compile(r"(?i)(password|secret|token|api_key)\s*[=:]\s*\S+"),
        severity="high",
        description_template="Generic secret assignment detected: {match}",
        event_types=_BOTH,
        category="credentials",
    ),
    DetectionRule(
        rule_id="cred.bearer_token",
        label="credential_exposure",
        pattern=re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
        severity="high",
        description_template="Bearer token detected: {match}",
        event_types=_BOTH,
        category="credentials",
    ),
    DetectionRule(
        rule_id="cred.private_key_header",
        label="credential_exposure",
        pattern=re.compile(r"-----BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY-----"),
        severity="high",
        description_template="Private key header detected: {match}",
        event_types=_BOTH,
        category="credentials",
    ),
    # -------------------------------------------------------------------------
    # Privilege escalation — high severity
    # -------------------------------------------------------------------------
    DetectionRule(
        rule_id="esc.sudo",
        label="privilege_escalation",
        pattern=re.compile(r"\bsudo\b"),
        severity="high",
        description_template="sudo command detected: {match}",
        event_types=_BOTH,
        category="escalation",
    ),
    DetectionRule(
        rule_id="esc.su_dash",
        label="privilege_escalation",
        pattern=re.compile(r"\bsu\s+-"),
        severity="high",
        description_template="su - (switch to root) detected: {match}",
        event_types=_BOTH,
        category="escalation",
    ),
    DetectionRule(
        rule_id="esc.pkexec",
        label="privilege_escalation",
        pattern=re.compile(r"\bpkexec\b"),
        severity="high",
        description_template="pkexec privilege escalation detected: {match}",
        event_types=_BOTH,
        category="escalation",
    ),
    # -------------------------------------------------------------------------
    # Destructive commands — critical severity
    # -------------------------------------------------------------------------
    DetectionRule(
        rule_id="dest.rm_rf",
        label="destructive_command",
        pattern=re.compile(r"\brm\s+-rf\b"),
        severity="critical",
        description_template="Recursive force-remove detected: {match}",
        event_types=_BOTH,
        category="destructive",
    ),
    DetectionRule(
        rule_id="dest.drop_table",
        label="destructive_command",
        pattern=re.compile(r"(?i)\bDROP\s+(TABLE|DATABASE)\b"),
        severity="critical",
        description_template="SQL DROP statement detected: {match}",
        event_types=_BOTH,
        category="destructive",
    ),
    DetectionRule(
        rule_id="dest.kubectl_delete",
        label="destructive_command",
        pattern=re.compile(r"\bkubectl\s+delete\b"),
        severity="critical",
        description_template="kubectl delete command detected: {match}",
        event_types=_BOTH,
        category="destructive",
    ),
    DetectionRule(
        rule_id="dest.dd_if",
        label="destructive_command",
        pattern=re.compile(r"\bdd\s+if="),
        severity="critical",
        description_template="dd disk-copy command detected: {match}",
        event_types=_BOTH,
        category="destructive",
    ),
    DetectionRule(
        rule_id="dest.mkfs",
        label="destructive_command",
        pattern=re.compile(r"\bmkfs\."),
        severity="critical",
        description_template="mkfs (format filesystem) detected: {match}",
        event_types=_BOTH,
        category="destructive",
    ),
    # -------------------------------------------------------------------------
    # Outbound connections — info severity
    # -------------------------------------------------------------------------
    DetectionRule(
        rule_id="conn.ssh",
        label="outbound_connection",
        pattern=re.compile(r"\bssh\s+\w+@"),
        severity="info",
        description_template="SSH connection detected: {match}",
        event_types=_BOTH,
        category="connections",
    ),
    DetectionRule(
        rule_id="conn.curl",
        label="outbound_connection",
        pattern=re.compile(r"\bcurl\s+https?://"),
        severity="info",
        description_template="curl HTTP request detected: {match}",
        event_types=_BOTH,
        category="connections",
    ),
    DetectionRule(
        rule_id="conn.wget",
        label="outbound_connection",
        pattern=re.compile(r"\bwget\s+https?://"),
        severity="info",
        description_template="wget HTTP request detected: {match}",
        event_types=_BOTH,
        category="connections",
    ),
    DetectionRule(
        rule_id="conn.scp",
        label="outbound_connection",
        pattern=re.compile(r"\bscp\b"),
        severity="info",
        description_template="scp file transfer detected: {match}",
        event_types=_BOTH,
        category="connections",
    ),
    # -------------------------------------------------------------------------
    # Session lifecycle — info severity
    # -------------------------------------------------------------------------
    DetectionRule(
        rule_id="life.exit",
        label="session_lifecycle",
        pattern=re.compile(r"\bexit\b"),
        severity="info",
        description_template="exit command detected: {match}",
        event_types=_BOTH,
        category="lifecycle",
    ),
    DetectionRule(
        rule_id="life.shutdown",
        label="session_lifecycle",
        pattern=re.compile(r"\bshutdown\b"),
        severity="info",
        description_template="shutdown command detected: {match}",
        event_types=_BOTH,
        category="lifecycle",
    ),
    DetectionRule(
        rule_id="life.reboot",
        label="session_lifecycle",
        pattern=re.compile(r"\breboot\b"),
        severity="info",
        description_template="reboot command detected: {match}",
        event_types=_BOTH,
        category="lifecycle",
    ),
]

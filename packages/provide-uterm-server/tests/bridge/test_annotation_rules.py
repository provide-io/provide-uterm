#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for built-in annotation detection rules."""

from __future__ import annotations

import pytest

from provide.terminal.bridge.annotation._rules import BUILTIN_RULES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(rule_id: str):  # type: ignore[return]
    """Return the DetectionRule with the given rule_id, failing the test if absent."""
    for r in BUILTIN_RULES:
        if r.rule_id == rule_id:
            return r
    pytest.fail(f"Rule {rule_id!r} not found in BUILTIN_RULES")


def _matches(rule_id: str, text: str) -> bool:
    """Return True if the rule with rule_id matches text."""
    return _rule(rule_id).pattern.search(text) is not None


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------


def test_builtin_rules_count() -> None:
    assert len(BUILTIN_RULES) == 20


def test_all_five_categories_present() -> None:
    categories = {r.category for r in BUILTIN_RULES}
    assert categories == {"credentials", "escalation", "destructive", "connections", "lifecycle"}


def test_all_rules_have_both_event_types() -> None:
    for rule in BUILTIN_RULES:
        assert rule.event_types == frozenset({"read", "send"}), f"Rule {rule.rule_id} has unexpected event_types"


def test_rule_ids_are_unique() -> None:
    ids = [r.rule_id for r in BUILTIN_RULES]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Credentials — positive matches
# ---------------------------------------------------------------------------


def test_aws_access_key_matches_valid_key() -> None:
    assert _matches("cred.aws_access_key", "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")


def test_aws_access_key_matches_bare_key() -> None:
    assert _matches("cred.aws_access_key", "AKIAIOSFODNN7EXAMPLE")


def test_github_pat_token_matches() -> None:
    assert _matches("cred.github_token", "ghp_" + "A" * 36)


def test_github_server_token_matches() -> None:
    assert _matches("cred.github_token", "ghs_" + "b" * 40)


def test_generic_secret_password_equals() -> None:
    assert _matches("cred.generic_secret", "password=hunter2")


def test_generic_secret_token_colon() -> None:
    assert _matches("cred.generic_secret", "TOKEN: supersecretvalue123")


def test_bearer_token_matches_jwt() -> None:
    assert _matches("cred.bearer_token", "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")


def test_bearer_token_matches_simple() -> None:
    assert _matches("cred.bearer_token", "Bearer eyJhbGciOiJIUzI1NiJ9.test")


def test_private_key_rsa_header_matches() -> None:
    assert _matches("cred.private_key_header", "-----BEGIN RSA PRIVATE KEY-----")


def test_private_key_openssh_header_matches() -> None:
    assert _matches("cred.private_key_header", "-----BEGIN OPENSSH PRIVATE KEY-----")


# ---------------------------------------------------------------------------
# Credentials — negative match
# ---------------------------------------------------------------------------


def test_normal_text_does_not_match_credentials() -> None:
    text = "Hello world, this is a normal terminal output line."
    for rule in BUILTIN_RULES:
        if rule.category == "credentials":
            assert rule.pattern.search(text) is None, f"Rule {rule.rule_id} falsely matched: {text!r}"


# ---------------------------------------------------------------------------
# Escalation — positive matches
# ---------------------------------------------------------------------------


def test_sudo_command_matches() -> None:
    assert _matches("esc.sudo", "sudo apt-get update")


def test_sudo_in_middle_of_line_matches() -> None:
    assert _matches("esc.sudo", "then sudo reboot")


def test_su_dash_matches() -> None:
    assert _matches("esc.su_dash", "su -")


def test_su_dash_username_matches() -> None:
    assert _matches("esc.su_dash", "su - root")


def test_pkexec_matches() -> None:
    assert _matches("esc.pkexec", "pkexec /usr/bin/gparted")


def test_pkexec_matches_standalone() -> None:
    assert _matches("esc.pkexec", "pkexec")


# ---------------------------------------------------------------------------
# Destructive — positive matches
# ---------------------------------------------------------------------------


def test_rm_rf_matches() -> None:
    assert _matches("dest.rm_rf", "rm -rf /tmp/build")


def test_rm_rf_root_matches() -> None:
    assert _matches("dest.rm_rf", "sudo rm -rf /")


def test_drop_table_matches() -> None:
    assert _matches("dest.drop_table", "DROP TABLE users;")


def test_drop_database_matches() -> None:
    assert _matches("dest.drop_table", "drop database mydb;")


def test_kubectl_delete_matches() -> None:
    assert _matches("dest.kubectl_delete", "kubectl delete pod mypod")


def test_kubectl_delete_namespace_matches() -> None:
    assert _matches("dest.kubectl_delete", "kubectl delete namespace staging")


def test_dd_if_matches() -> None:
    assert _matches("dest.dd_if", "dd if=/dev/urandom of=/dev/sda")


def test_mkfs_matches() -> None:
    assert _matches("dest.mkfs", "mkfs.ext4 /dev/sdb1")


def test_mkfs_vfat_matches() -> None:
    assert _matches("dest.mkfs", "mkfs.vfat /dev/sdc1")


# ---------------------------------------------------------------------------
# Connections — positive matches
# ---------------------------------------------------------------------------


def test_ssh_user_at_host_matches() -> None:
    assert _matches("conn.ssh", "ssh deploy@example.com")


def test_ssh_with_options_matches() -> None:
    assert _matches("conn.ssh", "ssh admin@192.168.1.1 -p 2222")


def test_curl_http_matches() -> None:
    assert _matches("conn.curl", "curl http://example.com/api")


def test_curl_https_matches() -> None:
    assert _matches("conn.curl", "curl https://api.github.com/repos")


def test_wget_https_matches() -> None:
    assert _matches("conn.wget", "wget https://releases.ubuntu.com/latest.iso")


def test_wget_http_matches() -> None:
    assert _matches("conn.wget", "wget http://example.com/file.tar.gz")


def test_scp_matches() -> None:
    assert _matches("conn.scp", "scp file.txt user@host:/remote/path/")


def test_scp_pull_matches() -> None:
    assert _matches("conn.scp", "scp user@host:/etc/passwd .")


# ---------------------------------------------------------------------------
# Lifecycle — positive matches
# ---------------------------------------------------------------------------


def test_exit_matches() -> None:
    assert _matches("life.exit", "exit")


def test_exit_with_code_matches() -> None:
    assert _matches("life.exit", "exit 1")


def test_shutdown_matches() -> None:
    assert _matches("life.shutdown", "shutdown -h now")


def test_shutdown_reboot_flag_matches() -> None:
    assert _matches("life.shutdown", "shutdown -r 5")


def test_reboot_matches() -> None:
    assert _matches("life.reboot", "reboot")


def test_reboot_now_matches() -> None:
    assert _matches("life.reboot", "sudo reboot now")

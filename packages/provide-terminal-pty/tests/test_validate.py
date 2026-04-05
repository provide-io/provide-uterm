# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from provide.terminal.pty._validate import (
    validate_command,
    validate_env,
    validate_service_name,
    validate_username,
)

# ── validate_command ──────────────────────────────────────────────────────


def test_absolute_path_accepted() -> None:
    validate_command("/bin/bash")  # must not raise


def test_relative_path_rejected() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        validate_command("bash")


def test_path_traversal_rejected() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        validate_command("../../../bin/bash")


def test_empty_command_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_command("")


def test_null_byte_in_command_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        validate_command("/bin/bash\x00evil")


def test_command_too_long_rejected() -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_command("/" + "a" * 4097)


# ── validate_username ─────────────────────────────────────────────────────


def test_valid_username_accepted() -> None:
    for name in ["alice", "bob123", "_service", "user-name"]:
        validate_username(name)  # must not raise


def test_empty_username_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_username("")


def test_username_with_null_byte_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        validate_username("alice\x00evil")


def test_username_too_long_rejected() -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_username("a" * 256)


def test_username_with_colon_rejected() -> None:
    with pytest.raises(ValueError, match="invalid character"):
        validate_username("ali:ce")


def test_username_with_slash_rejected() -> None:
    with pytest.raises(ValueError, match="invalid character"):
        validate_username("../root")


# ── validate_service_name ─────────────────────────────────────────────────


def test_valid_service_accepted() -> None:
    for svc in ["login", "sshd", "provide-terminal", "sudo"]:
        validate_service_name(svc)  # must not raise


def test_empty_service_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_service_name("")


def test_service_with_slash_rejected() -> None:
    with pytest.raises(ValueError, match="invalid character"):
        validate_service_name("../../etc/passwd")


def test_service_with_null_byte_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        validate_service_name("login\x00")


def test_service_too_long_rejected() -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_service_name("a" * 256)


# ── validate_env ──────────────────────────────────────────────────────────


def test_valid_env_accepted() -> None:
    validate_env({"HOME": "/home/alice", "PATH": "/usr/bin:/bin"})  # must not raise


def test_empty_env_accepted() -> None:
    validate_env({})  # must not raise


def test_env_key_with_null_byte_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        validate_env({"KEY\x00": "value"})


def test_env_value_with_null_byte_rejected() -> None:
    with pytest.raises(ValueError, match="null byte"):
        validate_env({"KEY": "val\x00ue"})


def test_env_key_with_equals_rejected() -> None:
    with pytest.raises(ValueError, match="invalid key"):
        validate_env({"KEY=BAD": "value"})


def test_env_too_many_keys_rejected() -> None:
    with pytest.raises(ValueError, match="too many"):
        validate_env({f"K{i}": "v" for i in range(1001)})


def test_env_value_too_long_rejected() -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_env({"KEY": "x" * 65537})

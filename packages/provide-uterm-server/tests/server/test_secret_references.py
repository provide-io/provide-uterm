from __future__ import annotations

import os
from pathlib import Path

import pytest

import provide.uterm.server.secrets as secrets_module
from provide.uterm.server.secrets import MAX_SECRET_BYTES, SecretReference, SecretResolutionError


def test_env_reference_is_lazy_bounded_bytes_and_serializes_as_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("env:GRAPHICAL_CERT")
    assert str(reference) == "env:GRAPHICAL_CERT"
    monkeypatch.setenv("GRAPHICAL_CERT", "certificate")
    assert reference.resolve() == b"certificate"
    monkeypatch.setenv("GRAPHICAL_CERT", "x" * (MAX_SECRET_BYTES + 1))
    with pytest.raises(SecretResolutionError, match="exceeds maximum size") as exc:
        reference.resolve()
    assert "xxx" not in str(exc.value)
    with pytest.raises(ValueError, match="positive"):
        reference.resolve(max_bytes=0)


def test_existing_reference_can_be_rebased_and_relative_needs_context(tmp_path: Path) -> None:
    reference = SecretReference.parse("file:key")
    assert SecretReference.parse(reference) is reference
    with pytest.raises(SecretResolutionError, match="no config directory"):
        reference.resolve()
    rebased = SecretReference.parse(reference, base_dir=tmp_path)
    assert rebased.base_dir == tmp_path


def test_file_reference_resolves_relative_to_config_directory(tmp_path: Path) -> None:
    secret = tmp_path / "keys" / "client.key"
    secret.parent.mkdir()
    secret.write_bytes(b"key-bytes")
    secret.chmod(0o600)
    reference = SecretReference.parse("file:keys/client.key", base_dir=tmp_path)
    assert str(reference) == "file:keys/client.key"
    assert reference.resolve() == b"key-bytes"


@pytest.mark.parametrize("text", ["", "SECRET", "env:", "env:bad-name", "file:", "vault:key"])
def test_invalid_reference_syntax_is_rejected_without_echoing_value(text: str) -> None:
    with pytest.raises(ValueError, match="secret reference") as exc:
        SecretReference.parse(text)
    if text:
        assert text not in str(exc.value)


@pytest.mark.parametrize("text", ["file:key\x00suffix", "file:key\nname", "env:SECRET\x7f"])
def test_reference_rejects_control_characters_without_leakage(text: str) -> None:
    with pytest.raises(ValueError, match="invalid secret reference") as exc:
        SecretReference.parse(text)
    assert text not in str(exc.value)


def test_missing_env_and_file_errors_are_stable_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERY_SENSITIVE_NAME", raising=False)
    with pytest.raises(SecretResolutionError) as env_error:
        SecretReference.parse("env:VERY_SENSITIVE_NAME").resolve()
    assert str(env_error.value) == "environment secret is unavailable"
    with pytest.raises(SecretResolutionError) as file_error:
        SecretReference.parse("file:secret-name", base_dir=tmp_path).resolve()
    assert str(file_error.value) == "file secret is unavailable"
    assert str(tmp_path) not in str(file_error.value)


def test_file_rejects_traversal_directory_symlink_and_oversize(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="unsafe relative"):
        SecretReference.parse("file:../outside-secret", base_dir=tmp_path)
    with pytest.raises(SecretResolutionError, match="regular file"):
        SecretReference.parse("file:.", base_dir=tmp_path).resolve()
    regular = tmp_path / "regular"
    regular.write_bytes(b"secret")
    regular.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(regular)
    with pytest.raises(SecretResolutionError, match="symbolic links"):
        SecretReference.parse("file:link", base_dir=tmp_path).resolve()
    real_directory = tmp_path / "real-directory"
    real_directory.mkdir()
    nested = real_directory / "key"
    nested.write_bytes(b"nested-secret")
    nested.chmod(0o600)
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(SecretResolutionError, match="symbolic links"):
        SecretReference.parse("file:linked/key", base_dir=tmp_path).resolve()
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * (MAX_SECRET_BYTES + 1))
    huge.chmod(0o600)
    with pytest.raises(SecretResolutionError, match="exceeds maximum size"):
        SecretReference.parse("file:huge", base_dir=tmp_path).resolve()


def test_absolute_file_and_growth_during_read_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "absolute"
    secret.write_bytes(b"abcdef")
    secret.chmod(0o600)
    reference = SecretReference.parse(f"file:{secret}")
    assert reference.resolve() == b"abcdef"
    real_fstat = os.fstat

    def hidden_size(descriptor: int) -> os.stat_result:
        result = list(real_fstat(descriptor))
        result[6] = 0
        return os.stat_result(result)

    monkeypatch.setattr(os, "fstat", hidden_size)
    with pytest.raises(SecretResolutionError, match="exceeds maximum size"):
        reference.resolve(max_bytes=3)


def test_file_fails_closed_when_secure_primitives_are_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("file:key", base_dir=tmp_path)
    monkeypatch.delattr(secrets_module.os, "O_NOFOLLOW")
    with pytest.raises(SecretResolutionError, match="secure file secrets are unsupported"):
        reference.resolve()


def test_file_redacts_unsupported_dir_fd_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("file:sensitive-name", base_dir=tmp_path)
    real_open = secrets_module.os.open

    def unsupported_open(path: object, flags: int, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None:
            raise NotImplementedError("sensitive-name leaked by platform")
        return real_open(path, flags)

    monkeypatch.setattr(secrets_module.os, "open", unsupported_open)
    monkeypatch.setattr(secrets_module.os, "supports_dir_fd", os.supports_dir_fd | {unsupported_open})
    with pytest.raises(SecretResolutionError, match="secure file secrets are unsupported") as exc:
        reference.resolve()
    assert "sensitive-name" not in str(exc.value)


def test_file_redacts_unsupported_anchor_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("file:anchor-secret", base_dir=tmp_path)
    real_open = secrets_module.os.open

    def unsupported_anchor(path: object, flags: int, *, dir_fd: int | None = None) -> int:
        if dir_fd is None:
            raise TypeError("anchor-secret platform failure")
        return real_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(secrets_module.os, "open", unsupported_anchor)
    monkeypatch.setattr(secrets_module.os, "supports_dir_fd", os.supports_dir_fd | {unsupported_anchor})
    with pytest.raises(SecretResolutionError) as exc:
        reference.resolve()
    assert str(exc.value) == "secure file secrets are unsupported on this platform"
    assert "anchor-secret" not in str(exc.value)


def test_file_redacts_missing_base_directory_anchor(tmp_path: Path) -> None:
    missing_base = tmp_path / "deleted-sensitive-base"
    reference = SecretReference.parse("file:key", base_dir=missing_base)
    with pytest.raises(SecretResolutionError) as exc:
        reference.resolve()
    assert str(exc.value) == "file secret is unavailable"
    assert str(missing_base) not in str(exc.value)


def test_file_redacts_anchor_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("file:key", base_dir=tmp_path)

    def denied_anchor(path: object, flags: int, *, dir_fd: int | None = None) -> int:
        raise PermissionError(f"denied sensitive anchor {path}")

    monkeypatch.setattr(secrets_module.os, "open", denied_anchor)
    monkeypatch.setattr(secrets_module.os, "supports_dir_fd", os.supports_dir_fd | {denied_anchor})
    with pytest.raises(SecretResolutionError) as exc:
        reference.resolve()
    assert str(exc.value) == "file secret is unavailable"
    assert str(tmp_path) not in str(exc.value)


def test_file_redacts_unsupported_fallback_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reference = SecretReference.parse("file:stat-secret", base_dir=tmp_path)
    real_open = secrets_module.os.open
    real_stat = secrets_module.os.stat

    def missing_open(path: object, flags: int, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None:
            raise FileNotFoundError("stat-secret missing")
        return real_open(path, flags)

    def unsupported_stat(path: object, *, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
        if dir_fd is not None:
            raise NotImplementedError("stat-secret platform failure")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(secrets_module.os, "open", missing_open)
    monkeypatch.setattr(secrets_module.os, "stat", unsupported_stat)
    monkeypatch.setattr(secrets_module.os, "supports_dir_fd", os.supports_dir_fd | {missing_open, unsupported_stat})
    monkeypatch.setattr(secrets_module.os, "supports_follow_symlinks", os.supports_follow_symlinks | {unsupported_stat})
    with pytest.raises(SecretResolutionError) as exc:
        reference.resolve()
    assert str(exc.value) == "secure file secrets are unsupported on this platform"
    assert "stat-secret" not in str(exc.value)


def test_env_reference_works_without_file_primitives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTABLE_SECRET", "portable")
    monkeypatch.delattr(secrets_module.os, "O_NOFOLLOW")
    assert SecretReference.parse("env:PORTABLE_SECRET").resolve() == b"portable"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership/mode checks")
def test_file_rejects_group_or_world_permissions_and_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "loose"
    secret.write_bytes(b"secret")
    secret.chmod(0o644)
    reference = SecretReference.parse("file:loose", base_dir=tmp_path)
    with pytest.raises(SecretResolutionError, match="permissions"):
        reference.resolve()
    secret.chmod(0o600)
    real_fstat = os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        values = list(result)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(SecretResolutionError, match="ownership"):
        reference.resolve()

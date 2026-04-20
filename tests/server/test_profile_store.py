#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for ConnectionProfile model and FileProfileStore."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from provide.terminal.server.profiles import ConnectionProfile, FileProfileStore


def _make_profile(
    profile_id: str = "profile-abc123",
    owner: str = "user1",
    name: str = "My Server",
    connector_type: str = "ssh",
    visibility: str = "private",
    **kwargs: object,
) -> ConnectionProfile:
    now = time.time()
    return ConnectionProfile(
        profile_id=profile_id,
        owner=owner,
        name=name,
        connector_type=connector_type,
        visibility=visibility,
        created_at=now,
        updated_at=now,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture()
def store(tmp_path: Path) -> FileProfileStore:
    return FileProfileStore(tmp_path / "profiles")


# ── List ──────────────────────────────────────────────────────────────────


async def test_list_returns_empty_when_file_missing(store: FileProfileStore) -> None:
    result = await store.list_profiles()
    assert result == []


async def test_list_returns_own_private_profiles(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user1", visibility="private"))
    result = await store.list_profiles(owner="user1")
    assert len(result) == 1


async def test_list_excludes_other_private_profiles(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user2", visibility="private"))
    result = await store.list_profiles(owner="user1")
    assert result == []


async def test_list_includes_shared_profiles_for_any_owner(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user2", visibility="shared"))
    result = await store.list_profiles(owner="user1")
    assert len(result) == 1


async def test_list_with_no_owner_returns_all(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(profile_id="p1", owner="user1", visibility="private"))
    await store.create_profile(_make_profile(profile_id="p2", owner="user2", visibility="private"))
    result = await store.list_profiles()
    assert len(result) == 2


# ── Get ───────────────────────────────────────────────────────────────────


async def test_get_returns_profile_by_id(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    fetched = await store.get_profile(profile.profile_id)
    assert fetched is not None
    assert fetched.profile_id == profile.profile_id
    assert fetched.name == "My Server"


async def test_get_returns_none_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.get_profile("nonexistent")
    assert result is None


# ── Create ────────────────────────────────────────────────────────────────


async def test_create_persists_to_disk(store: FileProfileStore, tmp_path: Path) -> None:
    await store.create_profile(_make_profile())
    # Re-create store pointing to same directory — forces a fresh disk read.
    store2 = FileProfileStore(tmp_path / "profiles")
    result = await store2.list_profiles()
    assert len(result) == 1
    assert result[0].name == "My Server"


async def test_create_directory_created_if_missing(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path / "deep" / "nested" / "dir")
    await store.create_profile(_make_profile())
    assert (tmp_path / "deep" / "nested" / "dir" / "profiles.json").exists()


# ── Update ────────────────────────────────────────────────────────────────


async def test_update_changes_name(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    updated = await store.update_profile(profile.profile_id, {"name": "Renamed"})
    assert updated is not None
    assert updated.name == "Renamed"
    # Verify updated_at is refreshed
    assert updated.updated_at >= profile.updated_at


async def test_update_returns_none_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.update_profile("nonexistent", {"name": "x"})
    assert result is None


async def test_update_persists_change(store: FileProfileStore, tmp_path: Path) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    await store.update_profile(profile.profile_id, {"name": "Persisted"})
    store2 = FileProfileStore(tmp_path / "profiles")
    fetched = await store2.get_profile(profile.profile_id)
    assert fetched is not None
    assert fetched.name == "Persisted"


# ── Delete ────────────────────────────────────────────────────────────────


async def test_delete_removes_profile(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    deleted = await store.delete_profile(profile.profile_id)
    assert deleted is True
    assert await store.get_profile(profile.profile_id) is None


async def test_delete_returns_false_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.delete_profile("nonexistent")
    assert result is False


# ── Atomic write ──────────────────────────────────────────────────────────


async def test_atomic_write_no_tmp_file_left(store: FileProfileStore, tmp_path: Path) -> None:
    await store.create_profile(_make_profile())
    tmp = tmp_path / "profiles" / "profiles.tmp"
    assert not tmp.exists()


# ── Concurrency ───────────────────────────────────────────────────────────


async def test_concurrent_creates_are_consistent(store: FileProfileStore) -> None:
    profiles = [_make_profile(profile_id=f"profile-{i}", name=f"Server {i}") for i in range(10)]
    await asyncio.gather(*[store.create_profile(p) for p in profiles])
    all_profiles = await store.list_profiles()
    assert len(all_profiles) == 10


# ── Corrupt store ──────────────────────────────────────────────────────────


async def test_read_corrupt_json_raises_runtime_error(store: FileProfileStore, tmp_path: Path) -> None:
    """_read_sync raises RuntimeError when the profiles file contains corrupt JSON."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "profiles.json").write_text("not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Profiles store is corrupt"):
        await store.list_profiles()


async def test_update_skips_non_matching_profiles_and_returns_none(store: FileProfileStore) -> None:
    """update_profile iterates past non-matching profiles and returns None when not found."""
    # Create a profile so the for loop has entries to iterate over
    await store.create_profile(_make_profile(profile_id="profile-existing"))
    # Try to update a different ID — loop iterates profile-existing (no match) → returns None
    result = await store.update_profile("profile-nonexistent", {"name": "x"})
    assert result is None

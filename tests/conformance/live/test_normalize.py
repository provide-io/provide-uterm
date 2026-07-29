#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for masking and comparing what two cells of the matrix observed.

The comparison is the point of the whole harness: a Go client talking to a
Python server has to see what a Python client talking to a Go server sees. So
what counts as a difference — and what is allowed to differ — is decided here.
"""

from __future__ import annotations

from harness.normalize import VOLATILE, differences, mask, observations


class TestMask:
    def test_replaces_a_declared_path(self) -> None:
        assert mask({"body": {"id": "abc"}}, ["body.id"]) == {"body": {"id": VOLATILE}}

    def test_leaves_everything_else_alone(self) -> None:
        masked = mask({"status": 200, "body": {"id": "abc", "kind": "shell"}}, ["body.id"])
        assert masked == {"status": 200, "body": {"id": VOLATILE, "kind": "shell"}}

    def test_does_not_change_what_it_was_given(self) -> None:
        original = {"body": {"id": "abc"}}
        mask(original, ["body.id"])
        assert original == {"body": {"id": "abc"}}

    def test_a_path_that_is_not_there_is_left_absent(self) -> None:
        # Masking an absent field into existence would hide the difference
        # between a server that sent it and one that did not — which is
        # exactly the kind of drift this harness exists to catch.
        assert mask({"body": {}}, ["body.id"]) == {"body": {}}

    def test_masks_inside_a_list_by_index(self) -> None:
        masked = mask({"body": {"sessions": [{"id": "a"}, {"id": "b"}]}}, ["body.sessions.0.id"])
        assert masked == {"body": {"sessions": [{"id": VOLATILE}, {"id": "b"}]}}

    def test_a_star_masks_every_element(self) -> None:
        # A session list nobody can predict the length of still has to be
        # comparable on everything except the ids.
        masked = mask({"body": {"sessions": [{"id": "a"}, {"id": "b"}]}}, ["body.sessions.*.id"])
        assert masked == {"body": {"sessions": [{"id": VOLATILE}, {"id": VOLATILE}]}}

    def test_a_star_over_a_mapping_masks_every_value(self) -> None:
        masked = mask({"body": {"by_id": {"a": 1, "b": 2}}}, ["body.by_id.*"])
        assert masked == {"body": {"by_id": {"a": VOLATILE, "b": VOLATILE}}}

    def test_masking_through_a_scalar_changes_nothing(self) -> None:
        assert mask({"body": "text"}, ["body.id"]) == {"body": "text"}


class TestDifferences:
    def test_two_identical_observations_differ_nowhere(self) -> None:
        assert differences({"status": 200}, {"status": 200}) == []

    def test_names_the_path_that_differs(self) -> None:
        (only,) = differences({"body": {"status": "ok"}}, {"body": {"status": "OK"}})
        assert only.path == "body.status"
        assert only.left == "ok"
        assert only.right == "OK"

    def test_a_field_one_side_did_not_send_is_a_difference(self) -> None:
        (only,) = differences({"body": {"a": 1}}, {"body": {}})
        assert only.path == "body.a"

    def test_reports_every_difference_not_just_the_first(self) -> None:
        # A run that stopped at the first divergence would take four rounds to
        # show what one round already knows.
        found = differences({"a": 1, "b": 2}, {"a": 9, "b": 8})
        assert sorted(one.path for one in found) == ["a", "b"]

    def test_a_list_that_is_shorter_on_one_side_differs_at_the_index(self) -> None:
        found = differences({"a": [1, 2]}, {"a": [1]})
        assert [one.path for one in found] == ["a.1"]

    def test_a_boolean_is_not_the_number_one(self) -> None:
        assert differences({"ok": True}, {"ok": 1}) != []

    def test_a_whole_float_is_the_same_number(self) -> None:
        # One language's JSON reader hands back 200, another 200.0. The wire
        # carried the same number, so this is not a divergence.
        assert differences({"status": 200}, {"status": 200.0}) == []

    def test_a_type_change_is_reported_at_the_path_not_inside_it(self) -> None:
        found = differences({"a": {"b": 1}}, {"a": "text"})
        assert [one.path for one in found] == ["a"]


class TestObservations:
    def test_keys_the_steps_by_id_and_masks_each(self) -> None:
        result = {
            "steps": [
                {"id": "create", "fields": {"status": 200, "body": {"id": "s-1"}}},
                {"id": "health", "fields": {"status": 200}},
            ]
        }
        seen = observations(result, {"create": ["body.id"]})
        assert seen == {"create": {"status": 200, "body": {"id": VOLATILE}}, "health": {"status": 200}}

    def test_a_step_with_nothing_declared_volatile_is_carried_whole(self) -> None:
        result = {"steps": [{"id": "health", "fields": {"status": 200}}]}
        assert observations(result, {}) == {"health": {"status": 200}}

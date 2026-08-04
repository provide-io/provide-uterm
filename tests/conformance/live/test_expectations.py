#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the harness's expectation checker.

Every scenario's expectations are evaluated here and nowhere else — that is
what makes four languages' results comparable, so this is the one place where
"what an expectation means" is decided. It has to be exact about the things
JSON and Python disagree on: ``True == 1`` is true in Python and false in
every reading of the contract.
"""

from __future__ import annotations

import pytest
from harness.expectations import (
    MISSING,
    Expectation,
    check,
    check_all,
    parse_expectation,
    resolve,
)


class TestResolve:
    """The dotted path into a step's recorded fields."""

    def test_reads_a_top_level_field(self) -> None:
        assert resolve({"status": 200}, "status") == 200

    def test_reads_through_a_nested_object(self) -> None:
        assert resolve({"body": {"status": "ok"}}, "body.status") == "ok"

    def test_indexes_a_list_by_number(self) -> None:
        assert resolve({"body": {"sessions": [{"id": "a"}]}}, "body.sessions.0.id") == "a"

    def test_a_numeric_key_on_a_mapping_is_a_key_not_an_index(self) -> None:
        # A server is free to return an object keyed by digits. Reading it as
        # a list index would silently look at the wrong thing.
        assert resolve({"body": {"0": "by-key"}}, "body.0") == "by-key"

    def test_a_missing_key_is_missing_rather_than_none(self) -> None:
        # A field that is absent and a field whose value is null are different
        # observations, and a scenario must be able to tell them apart.
        assert resolve({"body": {}}, "body.status") is MISSING
        assert resolve({"body": {"status": None}}, "body.status") is None

    def test_an_index_past_the_end_is_missing(self) -> None:
        assert resolve({"body": [1]}, "body.5") is MISSING

    def test_a_negative_index_is_missing_rather_than_the_tail(self) -> None:
        # Python would read -1 as the last element. The path grammar has no
        # negative index, so this is a malformed path, not a clever one.
        assert resolve({"body": [1, 2]}, "body.-1") is MISSING

    def test_descending_into_a_scalar_is_missing(self) -> None:
        assert resolve({"body": "text"}, "body.status") is MISSING

    def test_the_empty_path_is_the_fields_themselves(self) -> None:
        fields = {"status": 200}
        assert resolve(fields, "") == fields


class TestEquals:
    """The predicate every scenario leans on."""

    def test_holds_on_an_equal_value(self) -> None:
        assert check(_exp("s", "status", "equals", 200), {"s": {"status": 200}}) is None

    def test_fails_and_reports_what_it_saw(self) -> None:
        failure = check(_exp("s", "status", "equals", 200), {"s": {"status": 401}})
        assert failure is not None
        assert failure.actual == 401
        assert failure.expected == 200

    def test_true_is_not_one(self) -> None:
        # Python's bool is an int. JSON's is not, and neither is the contract.
        assert check(_exp("s", "ok", "equals", 1), {"s": {"ok": True}}) is not None
        assert check(_exp("s", "ok", "equals", True), {"s": {"ok": 1}}) is not None

    def test_true_equals_true(self) -> None:
        assert check(_exp("s", "ok", "equals", True), {"s": {"ok": True}}) is None

    def test_a_whole_float_equals_the_integer(self) -> None:
        # JSON has one number type; 200 and 200.0 are the same observation.
        assert check(_exp("s", "status", "equals", 200), {"s": {"status": 200.0}}) is None

    def test_compares_a_whole_structure(self) -> None:
        assert check(_exp("s", "body", "equals", {"a": [1]}), {"s": {"body": {"a": [1]}}}) is None

    def test_a_missing_value_fails_rather_than_raising(self) -> None:
        failure = check(_exp("s", "body.status", "equals", "ok"), {"s": {"body": {}}})
        assert failure is not None
        assert failure.actual is MISSING


class TestOtherPredicates:
    def test_in_accepts_a_member(self) -> None:
        assert check(_exp("s", "status", "in", [401, 403]), {"s": {"status": 403}}) is None

    def test_in_refuses_a_non_member(self) -> None:
        assert check(_exp("s", "status", "in", [401, 403]), {"s": {"status": 200}}) is not None

    def test_in_does_not_read_true_as_one(self) -> None:
        assert check(_exp("s", "ok", "in", [1]), {"s": {"ok": True}}) is not None

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("string", "a"),
            ("number", 1),
            ("number", 1.5),
            ("boolean", True),
            ("array", []),
            ("object", {}),
            ("null", None),
        ],
    )
    def test_type_names_the_json_type(self, name: str, value: object) -> None:
        assert check(_exp("s", "v", "type", name), {"s": {"v": value}}) is None

    def test_type_does_not_call_a_boolean_a_number(self) -> None:
        assert check(_exp("s", "v", "type", "number"), {"s": {"v": True}}) is not None

    def test_type_of_a_missing_value_is_not_null(self) -> None:
        # Absent is not null — a server that dropped the field entirely has
        # done something different from one that sent null.
        assert check(_exp("s", "v", "type", "null"), {"s": {}}) is not None

    def test_matches_finds_the_pattern_anywhere(self) -> None:
        assert (
            check(_exp("s", "body.detail", "matches", "not.*found"), {"s": {"body": {"detail": "was not found"}}})
            is None
        )

    def test_matches_fails_on_a_non_string(self) -> None:
        # Coercing a number to text here would let a scenario match `200`
        # against a pattern written for a message.
        assert check(_exp("s", "v", "matches", "2"), {"s": {"v": 200}}) is not None

    def test_min_count_counts_a_list(self) -> None:
        assert check(_exp("s", "body.sessions", "min_count", 0), {"s": {"body": {"sessions": []}}}) is None
        assert check(_exp("s", "body.sessions", "min_count", 2), {"s": {"body": {"sessions": [1]}}}) is not None

    def test_min_count_refuses_something_with_no_length(self) -> None:
        assert check(_exp("s", "v", "min_count", 1), {"s": {"v": 5}}) is not None

    def test_present_true_wants_any_value_including_null(self) -> None:
        assert check(_exp("s", "v", "present", True), {"s": {"v": None}}) is None
        assert check(_exp("s", "v", "present", True), {"s": {}}) is not None

    def test_present_false_wants_the_field_gone(self) -> None:
        assert check(_exp("s", "v", "present", False), {"s": {}}) is None
        assert check(_exp("s", "v", "present", False), {"s": {"v": None}}) is not None


class TestStepLookup:
    def test_an_expectation_naming_a_step_nobody_ran_fails(self) -> None:
        # A typo in a scenario would otherwise pass every cell in the matrix,
        # which is the worst way for a check to be wrong.
        failure = check(_exp("typo", "status", "equals", 200), {"s": {"status": 200}})
        assert failure is not None
        assert "typo" in failure.message


class TestParse:
    def test_reads_the_predicate_out_of_the_scenario_form(self) -> None:
        expectation = parse_expectation({"step": "s", "path": "status", "equals": 200, "why": "because"})
        assert expectation.predicate == "equals"
        assert expectation.expected == 200
        assert expectation.why == "because"

    def test_an_expectation_with_no_predicate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no predicate"):
            parse_expectation({"step": "s", "path": "status"})

    def test_an_expectation_with_two_predicates_is_refused(self) -> None:
        # Which one wins would be an implementation detail deciding a contract.
        with pytest.raises(ValueError, match="one predicate"):
            parse_expectation({"step": "s", "path": "status", "equals": 200, "type": "number"})

    def test_a_false_predicate_value_still_counts_as_given(self) -> None:
        # `"present": false` is a predicate, not an absent one.
        assert parse_expectation({"step": "s", "path": "v", "present": False}).predicate == "present"


class TestAnyRepetition:
    """``<step>.*`` — an expectation about *some* repetition rather than a named one.

    A repeated step exists precisely where the answers stop being the same, and
    some of those sequences are timed rather than counted. Pinning an index into
    one asserts an artifact of how fast the runner was, which is how
    ``008_rate_limits`` came to fail on a loaded machine and pass on a quiet one.
    """

    def test_holds_when_one_repetition_satisfies_it(self) -> None:
        steps = {
            "flood.0": {"status": 409},
            "flood.1": {"status": 429},
            "flood.2": {"status": 409},
        }

        assert check_all((_exp("flood.*", "status", "equals", 429),), steps) == ()

    def test_fails_when_no_repetition_satisfies_it(self) -> None:
        steps = {"flood.0": {"status": 409}, "flood.1": {"status": 409}}

        failures = check_all((_exp("flood.*", "status", "equals", 429),), steps)

        assert len(failures) == 1
        assert "no repetition of 'flood'" in failures[0].message

    def test_one_repetition_must_satisfy_every_expectation_about_it(self) -> None:
        # Split across two repetitions is not the same claim: the contract is
        # that a refusal carries its own reason, not that a 429 happened
        # somewhere and the word "rate_limited" happened somewhere else.
        steps = {
            "flood.0": {"status": 429, "body": {"error": "other"}},
            "flood.1": {"status": 409, "body": {"error": "rate_limited"}},
        }

        failures = check_all(
            (
                _exp("flood.*", "status", "equals", 429),
                _exp("flood.*", "body", "equals", {"error": "rate_limited"}),
            ),
            steps,
        )

        assert len(failures) == 1

    def test_holds_when_a_single_repetition_satisfies_all_of_them(self) -> None:
        steps = {
            "flood.0": {"status": 409, "body": {"error": "other"}},
            "flood.1": {"status": 429, "body": {"error": "rate_limited"}},
        }

        assert (
            check_all(
                (
                    _exp("flood.*", "status", "equals", 429),
                    _exp("flood.*", "body", "equals", {"error": "rate_limited"}),
                ),
                steps,
            )
            == ()
        )

    def test_fails_when_the_step_recorded_nothing(self) -> None:
        # A wildcard over zero observations must not read as satisfied — that is
        # the one failure shape that passes in every cell at once.
        failures = check_all((_exp("flood.*", "status", "equals", 429),), {"other": {"status": 200}})

        assert len(failures) == 1
        assert "no repetition of 'flood'" in failures[0].message

    def test_named_repetitions_are_unaffected(self) -> None:
        steps = {"flood.0": {"status": 409}, "flood.1": {"status": 429}}

        assert check_all((_exp("flood.0", "status", "equals", 409),), steps) == ()
        assert len(check_all((_exp("flood.0", "status", "equals", 429),), steps)) == 1

    def test_a_wildcard_does_not_match_a_differently_named_step(self) -> None:
        # `flood.*` must not pick up `floodgate.0`: the separator is part of the
        # prefix, not decoration.
        failures = check_all((_exp("flood.*", "status", "equals", 429),), {"floodgate.0": {"status": 429}})

        assert len(failures) == 1


def _exp(step: str, path: str, predicate: str, expected: object) -> Expectation:
    return Expectation(step=step, path=path, predicate=predicate, expected=expected, why=None)


class TestRateLimitScenarioSurvivesASlowRunner:
    """The 008_rate_limits regression, replayed from the CI failure it caused.

    Run 30860934627 (2026-08-03) failed this scenario on two cells with
    ``spend_acquire.7.body: expected {'error': 'rate_limited'}, saw
    {'error': 'Hijack not available in open input mode.'}`` — the eighth
    acquire answered on its merits because the flood had taken long enough for
    the bucket to hand a token back. A re-run of the identical commit passed.
    """

    @staticmethod
    def _flood(first_refusal: int) -> dict[str, dict[str, object]]:
        """A recorded flood whose limiter engages at *first_refusal*.

        Before it, acquires are answered on their merits: the session is in open
        mode, so there is no lease to take. From it, the budget is gone.
        """
        recorded: dict[str, dict[str, object]] = {}
        for index in range(30):
            if index < first_refusal:
                recorded[f"spend_acquire.{index}"] = {
                    "status": 409,
                    "ok": False,
                    "body": {"error": "Hijack not available in open input mode."},
                }
            else:
                recorded[f"spend_acquire.{index}"] = {
                    "status": 429,
                    "ok": False,
                    "body": {"error": "rate_limited"},
                }
        recorded["unknown_worker_while_spent"] = {
            "status": 404,
            "body": {"detail": "unknown session: no-such-worker"},
        }
        recorded["anonymous_while_spent"] = {
            "status": 401,
            "body": {"detail": "authentication required"},
        }
        recorded["read_while_spent"] = {"status": 200, "body": []}
        return recorded

    @staticmethod
    def _expectations() -> tuple[Expectation, ...]:
        from harness.scenario import SCENARIO_DIR, load_scenario

        return tuple(load_scenario(SCENARIO_DIR / "008_rate_limits.json").expectations)

    def test_holds_on_a_quick_runner(self) -> None:
        # Five tokens spent, so the sixth acquire (index 5) is the first refused.
        assert check_all(self._expectations(), self._flood(first_refusal=5)) == ()

    def test_holds_when_a_refilled_token_pushes_the_refusal_later(self) -> None:
        # The CI failure: the flood was slow enough for the bucket to hand a
        # token back, so index 7 was answered on its merits. Pinning that index
        # asserted how fast the machine was.
        assert check_all(self._expectations(), self._flood(first_refusal=9)) == ()

    def test_still_fails_when_the_limiter_never_engages(self) -> None:
        # The property has to stay falsifiable: a server that never rate limits
        # must fail this scenario rather than pass it by never being observed.
        never = {name: dict(fields) for name, fields in self._flood(first_refusal=30).items()}

        failures = check_all(self._expectations(), never)

        assert failures, "a server that never refuses must not pass the rate-limit scenario"

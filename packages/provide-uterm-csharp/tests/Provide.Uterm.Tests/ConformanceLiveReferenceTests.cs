//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using Provide.Uterm.Conformance;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// The reference resolver: <c>${&lt;step id&gt;.&lt;dotted path&gt;}</c> read out of
/// what an earlier step recorded (<c>conformance/live/PROTOCOL.md</c>, "A step
/// that needs an earlier step's answer").
///
/// Four implementations of one small grammar is four chances to disagree, and
/// the failure this file is written against is the quiet one: a resolver whose
/// pattern never matches leaves every reference as written, and each step still
/// *runs* — asking a server about a literal <c>${...}</c>. So every assertion
/// here is that a reference produced a **value**, not that a code path executed.
/// </summary>
public sealed class ConformanceLiveReferenceTests
{
    /// <summary>What one step recorded, in the shape a driver writes down.</summary>
    private static JsonObject Fields(string bodyJson, int? status = 200, bool ok = true) => new()
    {
        ["status"] = status is null ? null : JsonValue.Create(status.Value),
        ["ok"] = ok,
        ["body"] = JsonNode.Parse(bodyJson),
        ["error"] = null,
    };

    private static LiveStep Step(string json) =>
        LiveScenario.Parse($$"""{"id": "s", "title": "T", "steps": [{{json}}], "expect": []}""").Steps[0];

    private static LiveStep Resolve(string json, params (string Id, JsonObject Fields)[] seen) =>
        LiveReference.Resolve(Step(json), seen.ToDictionary(e => e.Id, e => e.Fields, StringComparer.Ordinal));

    /// <summary>A step acting on the lease the step named <c>acquire</c> recorded.</summary>
    private static string SnapshotOf(string reference) =>
        $$"""{"id": "snap", "action": "hijack_snapshot", "worker_id": "w", "hijack_id": "{{reference}}"}""";

    [Fact]
    public void A_reference_resolves_to_the_lease_the_earlier_step_recorded()
    {
        var step = Resolve(
            SnapshotOf("${acquire.body.hijack_id}"),
            ("acquire", Fields("""{"hijack_id": "hj-1234", "owner": "operator"}""")));

        // The value, not the code path: a resolver that matched nothing would
        // leave the literal here and every test below would still pass.
        Assert.Equal("hj-1234", step.HijackId);
    }

    [Fact]
    public void The_pattern_only_matches_a_field_that_is_entirely_a_reference()
    {
        Assert.Matches(LiveReference.Pattern, "${acquire.body.hijack_id}");
        Assert.DoesNotMatch(LiveReference.Pattern, "a${acquire.body.hijack_id}b");
        // The doubled-backslash spelling: a pattern written that way matches a
        // literal backslash and resolves nothing while looking correct.
        Assert.DoesNotMatch(LiveReference.Pattern, @"\${acquire.body.hijack_id}");
    }

    [Fact]
    public void A_field_that_merely_contains_a_reference_is_sent_as_written()
    {
        var step = Resolve(
            """
            {"id": "send", "action": "hijack_send", "worker_id": "w", "hijack_id": "h",
             "keys": "id=${acquire.body.hijack_id}\n"}
            """,
            ("acquire", Fields("""{"hijack_id": "hj-1234"}""")));

        Assert.Equal("id=${acquire.body.hijack_id}\n", step.Keys);
    }

    [Fact]
    public void A_reference_naming_a_step_that_has_not_run_is_a_run_error()
    {
        var ex = Assert.Throws<LiveDriverException>(() => Resolve(SnapshotOf("${acquire.body.hijack_id}")));

        Assert.Contains("acquire", ex.Message, StringComparison.Ordinal);
        Assert.Contains("has not run", ex.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("${acquire.body.nope}")]
    [InlineData("${acquire.nope}")]
    [InlineData("${acquire.body.hijack_id.deeper}")]
    [InlineData("${acquire.body.owners.9}")]
    [InlineData("${acquire.body.owners.x}")]
    [InlineData("${acquire.error.anything}")]
    public void A_reference_to_a_path_that_is_not_there_is_a_run_error(string reference)
    {
        var ex = Assert.Throws<LiveDriverException>(() => Resolve(
            SnapshotOf(reference),
            ("acquire", Fields("""{"hijack_id": "hj-1234", "owners": ["a"]}"""))));

        Assert.Contains("is not there", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void A_dotted_path_digs_objects_by_key_and_arrays_by_index()
    {
        var step = Resolve(
            """{"id": "read", "action": "get_session", "session_id": "${listed.body.1.session_id}"}""",
            ("listed", Fields("""[{"session_id": "first"}, {"session_id": "second"}]""")));

        Assert.Equal("second", step.SessionId);
    }

    [Fact]
    public void A_resolved_number_reaches_the_wire_as_the_digits_the_server_sent()
    {
        // An id a server answered as a number is still that id. Rendering it as
        // text is the only way a string field can carry it.
        var step = Resolve(
            SnapshotOf("${acquire.body.hijack_id}"),
            ("acquire", Fields("""{"hijack_id": 4210}""")));

        Assert.Equal("4210", step.HijackId);
    }

    [Fact]
    public void The_status_and_the_ok_a_step_recorded_are_referenceable_too()
    {
        var step = Resolve(
            """{"id": "raw", "action": "http_get", "path": "${earlier.status}", "session_id": "${earlier.ok}"}""",
            ("earlier", Fields("{}", status: 409, ok: false)));

        Assert.Equal("409", step.Path);
        Assert.Equal("false", step.SessionId);
    }

    [Fact]
    public void A_field_a_step_recorded_as_null_resolves_to_null_rather_than_to_nothing()
    {
        // Absent and null are different answers, and only the first is a
        // malformed reference: a step that ran and recorded no error *has* an
        // error field, and its value is null.
        var step = Resolve(
            """{"id": "raw", "action": "http_get", "path": "${earlier.error}"}""",
            ("earlier", Fields("{}")));

        Assert.Null(step.Path);
    }

    [Fact]
    public void An_integer_field_takes_a_resolved_number()
    {
        var step = Resolve(
            """
            {"id": "beat", "action": "hijack_heartbeat", "worker_id": "w", "hijack_id": "h",
             "lease_s": "${acquire.body.lease_s}"}
            """,
            ("acquire", Fields("""{"lease_s": 45}""")));

        Assert.Equal(45, step.LeaseS);
    }

    [Fact]
    public void A_step_with_no_references_is_left_exactly_as_written()
    {
        var step = Resolve("""{"id": "beat", "action": "hijack_heartbeat", "worker_id": "w", "hijack_id": "h"}""");

        Assert.Equal("w", step.WorkerId);
        Assert.Equal("h", step.HijackId);
        Assert.Null(step.LeaseS);
    }

    [Fact]
    public void A_post_body_written_as_a_reference_is_substituted_as_json()
    {
        var step = Resolve(
            """{"id": "echo", "action": "http_post", "path": "/api/x", "body": "${earlier.body}"}""",
            ("earlier", Fields("""{"session_id": "demo"}""")));

        Assert.Equal("""{"session_id":"demo"}""", step.Body!.ToJsonString());
    }

    [Fact]
    public void Every_field_a_scenario_can_write_is_parsed()
    {
        var step = Step("""
        {"id": "send", "action": "hijack_send", "worker_id": "w", "hijack_id": "h",
         "owner": "auditor", "lease_s": 30, "keys": "echo hi\n", "input_mode": "hijack", "limit": 7}
        """);

        Assert.Equal("w", step.WorkerId);
        Assert.Equal("h", step.HijackId);
        Assert.Equal("auditor", step.Owner);
        Assert.Equal(30, step.LeaseS);
        Assert.Equal("echo hi\n", step.Keys);
        Assert.Equal("hijack", step.InputMode);
        Assert.Equal(7, step.Limit);
    }
}

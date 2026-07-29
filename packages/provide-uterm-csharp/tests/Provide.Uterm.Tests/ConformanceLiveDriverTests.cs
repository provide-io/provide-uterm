//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using Provide.Uterm.Conformance;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// The parts of the live driver that need no server: reading a scenario,
/// choosing an auth header, shaping a result.
///
/// A driver that misreads a scenario or mis-shapes a result would make every
/// cell of the matrix wrong in the same way, which is the failure nobody sees.
/// </summary>
public sealed class ConformanceLiveDriverScenarioTests
{
    private const string MinimalScenario = """
    {"id": "010_health", "title": "Health", "steps": [{"id": "health", "action": "health"}], "expect": []}
    """;

    [Fact]
    public void Parse_reads_the_steps_in_order()
    {
        var scenario = LiveScenario.Parse("""
        {"id": "020_two", "title": "Two", "steps": [
          {"id": "a", "action": "health"},
          {"id": "b", "action": "list_sessions"}
        ], "expect": []}
        """);

        Assert.Equal("020_two", scenario.Id);
        Assert.Equal("Two", scenario.Title);
        Assert.Equal(["a", "b"], scenario.Steps.Select(s => s.Id));
        Assert.Equal(["health", "list_sessions"], scenario.Steps.Select(s => s.Action));
    }

    [Fact]
    public void Parse_defaults_the_auth_mode_of_a_scenario_and_of_a_step()
    {
        var scenario = LiveScenario.Parse(MinimalScenario);

        Assert.Equal("dev_token", scenario.Auth);
        Assert.Equal(LiveStep.AuthToken, scenario.Steps[0].Auth);
        Assert.Empty(scenario.Requires);
    }

    [Fact]
    public void Parse_reads_a_named_auth_mode_and_required_capabilities()
    {
        var scenario = LiveScenario.Parse("""
        {"id": "030_jwt", "title": "T", "auth": "jwt", "requires": ["hijack.rest", 7],
         "steps": [{"id": "s", "action": "health", "auth": "none"}], "expect": []}
        """);

        Assert.Equal("jwt", scenario.Auth);
        // A non-string in `requires` is not a capability name; dropping it beats
        // inventing one that no driver can ever report.
        Assert.Equal(["hijack.rest"], scenario.Requires);
        Assert.Equal(LiveStep.AuthNone, scenario.Steps[0].Auth);
    }

    [Fact]
    public void Parse_keeps_the_post_body_and_the_path()
    {
        var scenario = LiveScenario.Parse("""
        {"id": "040_post", "title": "T", "steps": [
          {"id": "s", "action": "http_post", "path": "/api/sessions", "body": {"session_id": "demo"}}
        ], "expect": []}
        """);

        var step = scenario.Steps[0];
        Assert.Equal("/api/sessions", step.Path);
        Assert.Equal("""{"session_id":"demo"}""", step.Body!.ToJsonString());
    }

    [Fact]
    public void Parse_reads_repeat_and_leaves_a_step_without_one_at_a_single_run()
    {
        var scenario = LiveScenario.Parse("""
        {"id": "060_flood", "title": "T", "steps": [
          {"id": "once", "action": "health"},
          {"id": "flood", "action": "list_sessions", "repeat": 3}
        ], "expect": []}
        """);

        Assert.Equal(1, scenario.Steps[0].Repeat);
        Assert.Equal(3, scenario.Steps[1].Repeat);
    }

    [Fact]
    public void A_repeated_step_numbers_its_observations_from_zero()
    {
        var step = new LiveStep { Id = "flood", Action = "health", Repeat = 3 };

        // The bare `flood` records nothing: an expectation naming it would be
        // about a step nobody runs, which passes in every cell at once.
        Assert.Equal(["flood.0", "flood.1", "flood.2"], step.ObservationIds());
    }

    [Theory]
    [InlineData(1)]
    // Below the schema's floor of two, which the harness enforces before a
    // driver sees a scenario: one run under the bare id, not a renumbering.
    [InlineData(0)]
    public void A_step_that_runs_once_keeps_its_bare_id(int repeat)
    {
        var step = new LiveStep { Id = "health", Action = "health", Repeat = repeat };

        Assert.Equal(["health"], step.ObservationIds());
    }

    [Fact]
    public void Parse_names_an_id_less_scenario_after_its_file()
    {
        var scenario = LiveScenario.Parse(
            """{"title": "T", "steps": [{"id": "s", "action": "health"}], "expect": []}""",
            "050_fallback");

        Assert.Equal("050_fallback", scenario.Id);
    }

    [Theory]
    [InlineData("not json at all", "not JSON")]
    [InlineData("[1, 2]", "must be a JSON object")]
    [InlineData("""{"id": "x", "steps": []}""", "no steps")]
    [InlineData("""{"id": "x"}""", "no steps")]
    [InlineData("""{"id": "x", "steps": ["health"]}""", "must be a JSON object")]
    [InlineData("""{"id": "x", "steps": [{"action": "health"}]}""", "no id")]
    [InlineData("""{"id": "x", "steps": [{"id": "s"}]}""", "no action")]
    public void Parse_refuses_a_scenario_it_cannot_act_on(string json, string expected)
    {
        var ex = Assert.Throws<LiveDriverException>(() => LiveScenario.Parse(json));
        Assert.Contains(expected, ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Load_reads_a_file_and_reports_one_it_cannot()
    {
        var path = Path.Combine(Path.GetTempPath(), "live-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, MinimalScenario);
        try
        {
            Assert.Equal("010_health", LiveScenario.Load(path).Id);
        }
        finally
        {
            File.Delete(path);
        }

        var ex = Assert.Throws<LiveDriverException>(() => LiveScenario.Load(path));
        Assert.Contains("cannot read scenario", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Load_reports_a_path_that_is_not_a_file()
    {
        // A directory refuses to be read as text differently on each platform;
        // either way the driver says which scenario it could not run.
        var ex = Assert.Throws<LiveDriverException>(() => LiveScenario.Load(Path.GetTempPath()));

        Assert.Contains("cannot read scenario", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Auth_token_sends_the_bearer_the_server_driver_reported()
    {
        var headers = LiveClientDriver.AuthHeaders(LiveStep.AuthToken, "abc");

        Assert.Equal("Bearer abc", headers["Authorization"]);
    }

    [Fact]
    public void Auth_none_sends_no_authorization_header_at_all()
    {
        Assert.Empty(LiveClientDriver.AuthHeaders(LiveStep.AuthNone, "abc"));
    }

    [Fact]
    public void Auth_bad_sends_a_bearer_no_server_issued()
    {
        var headers = LiveClientDriver.AuthHeaders(LiveStep.AuthBad, "abc");

        Assert.Equal("Bearer " + LiveClientDriver.BadToken, headers["Authorization"]);
        Assert.NotEqual("Bearer abc", headers["Authorization"]);
    }

    [Fact]
    public void An_auth_mode_the_driver_does_not_know_is_a_driver_fault()
    {
        // Guessing (say, falling back to the token) would quietly run a
        // different scenario than the one that was written.
        Assert.Throws<LiveDriverException>(() => LiveClientDriver.AuthHeaders("cookie", "abc"));
    }

    [Fact]
    public void A_result_carries_exactly_the_fields_the_schema_names()
    {
        var result = new LiveResult
        {
            ScenarioId = "010_health",
            Status = LiveResult.StatusCompleted,
            Capabilities = LiveDriver.Capabilities,
            Steps =
            [
                new LiveStepResult { Id = "health", Status = 200, Ok = true, Body = JsonNode.Parse("""{"status":"ok"}""") },
            ],
        };

        var json = JsonNode.Parse(result.ToJsonLine())!.AsObject();

        Assert.Equal(
            ["scenario_id", "language", "role", "status", "capabilities", "steps", "error"],
            json.Select(kv => kv.Key));
        Assert.Equal("csharp", (string?)json["language"]);
        Assert.Equal("client", (string?)json["role"]);
        Assert.Null(json["error"]);
        var fields = json["steps"]![0]!["fields"]!.AsObject();
        Assert.Equal(200, (int?)fields["status"]);
        Assert.True((bool?)fields["ok"]);
        Assert.Equal("ok", (string?)fields["body"]!["status"]);
        Assert.Null(fields["error"]);
    }

    [Fact]
    public void A_step_that_got_no_answer_records_a_null_status()
    {
        var step = new LiveStepResult { Id = "s", Status = null, Ok = false, Error = "boom" };

        var fields = JsonNode.Parse(step.ToJson().ToJsonString())!["fields"]!;

        Assert.Null(fields["status"]);
        Assert.False((bool?)fields["ok"]);
        Assert.Equal("boom", (string?)fields["error"]);
    }

    [Fact]
    public void The_result_is_one_line_of_json()
    {
        var line = new LiveResult { ScenarioId = "x" }.ToJsonLine();

        Assert.DoesNotContain('\n', line);
    }

    [Fact]
    public void A_broken_stdin_ends_the_wait_the_same_way_eof_does()
    {
        // The harness may close its end abruptly; a driver that threw there
        // would be a server left running with nobody to stop it.
        LiveServeDriver.DrainToEof(new ThrowingStream(new IOException("pipe")));
        LiveServeDriver.DrainToEof(new ThrowingStream(new ObjectDisposedException("stdin")));
        LiveServeDriver.DrainToEof(new MemoryStream());
    }

    private sealed class ThrowingStream : Stream
    {
        private readonly Exception _failure;

        public ThrowingStream(Exception failure) => _failure = failure;

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();

        public override long Position
        {
            get => throw new NotSupportedException();
            set => throw new NotSupportedException();
        }

        public override int Read(byte[] buffer, int offset, int count) => throw _failure;

        public override void Flush()
        {
        }

        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }

    [Fact]
    public void The_driver_reports_that_it_observes_the_status_under_the_library()
    {
        // The capability is the promise that a 401, a 403 and a 404 arrive as
        // three observations rather than one `ok: false`.
        Assert.Contains("status.observed", LiveDriver.Capabilities);
    }
}

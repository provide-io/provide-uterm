//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Provide.Uterm.Conformance;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>Collects the driver's stdout and hands back its first line.</summary>
internal sealed class LiveLineWriter : TextWriter
{
    private readonly TaskCompletionSource<string> _first = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly StringBuilder _pending = new();

    public override Encoding Encoding => Encoding.UTF8;

    /// <summary>The one line of JSON the protocol says a driver writes.</summary>
    public Task<string> FirstLine => _first.Task;

    public override void Write(char value)
    {
        if (value == '\n')
        {
            _first.TrySetResult(_pending.ToString().TrimEnd('\r'));
            _pending.Clear();
            return;
        }

        _pending.Append(value);
    }
}

/// <summary>Stands in for stdin: blocks like a console does, then reaches EOF on demand.</summary>
internal sealed class LiveGatedStdin : Stream
{
    private readonly SemaphoreSlim _eof = new(0);

    public void ReachEof() => _eof.Release();

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();

    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override int Read(byte[] buffer, int offset, int count)
    {
        _eof.Wait();
        _eof.Release();
        return 0;
    }

    public override void Flush()
    {
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}

/// <summary>One `serve` role, started the way the harness starts it.</summary>
public sealed class LiveDriverServerFixture : IAsyncLifetime
{
    private readonly LiveGatedStdin _stdin = new();
    private Task<int>? _serving;
    private string? _previousTokenPath;
    private string _tokenPath = "";

    public JsonObject Handshake { get; private set; } = new();

    public string BaseUrl => (string?)Handshake["base_url"] ?? "";

    public string Token => (string?)Handshake["token"] ?? "";

    public async Task InitializeAsync()
    {
        _previousTokenPath = Environment.GetEnvironmentVariable("UTERM_DEV_TOKEN_PATH");
        _tokenPath = Path.Combine(Path.GetTempPath(), "live-dev-token-" + Guid.NewGuid().ToString("N"));
        Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", _tokenPath);
        var writer = new LiveLineWriter();
        _serving = LiveDriver.ExecuteAsync(["serve", "--auth", "dev_token"], writer, input: _stdin);
        var line = await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(60));
        Handshake = JsonNode.Parse(line)!.AsObject();
    }

    public async Task DisposeAsync()
    {
        _stdin.ReachEof();
        if (_serving is not null)
        {
            await _serving.WaitAsync(TimeSpan.FromSeconds(60));
        }

        Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", _previousTokenPath);
        File.Delete(_tokenPath);
    }
}

/// <summary>
/// The C# driver run end to end: its own `serve` role hosting the real server,
/// its `client` role running scenarios against it.
///
/// Nothing here asserts a verdict about the server — the harness owns every
/// expectation. What is asserted is that the driver *observed* accurately.
/// </summary>
public sealed class ConformanceLiveDriverServerTests : IClassFixture<LiveDriverServerFixture>
{
    private readonly LiveDriverServerFixture _server;

    public ConformanceLiveDriverServerTests(LiveDriverServerFixture server) => _server = server;

    [Fact]
    public void The_handshake_names_an_ephemeral_loopback_port_and_a_token()
    {
        Assert.Equal("server", (string?)_server.Handshake["role"]);
        Assert.Equal("csharp", (string?)_server.Handshake["language"]);
        var uri = new Uri(_server.BaseUrl);
        Assert.Equal(IPAddress.Loopback.ToString(), uri.Host);
        // The operating system chose it; nothing in the harness may name a port.
        Assert.True(uri.Port > 0);
        Assert.NotEmpty(_server.Token);
        Assert.Equal(LiveDriver.Capabilities, _server.Handshake["capabilities"]!.AsArray().Select(n => (string)n!));
    }

    [Fact]
    public async Task A_health_step_records_the_status_and_the_librarys_body()
    {
        var result = await RunAsync("""{"id": "health", "action": "health"}""");

        Assert.Equal("completed", (string?)result["status"]);
        var fields = Fields(result, "health");
        Assert.Equal(200, (int?)fields["status"]);
        Assert.True((bool?)fields["ok"]);
        Assert.Equal("ok", (string?)fields["body"]!["status"]);
        Assert.Equal("uterm-server", (string?)fields["body"]!["service"]);
        Assert.Null(fields["error"]);
    }

    [Fact]
    public async Task Three_different_refusals_stay_three_observations()
    {
        // Without the recording transport these would all be one `ok: false`,
        // and the matrix could not tell an anonymous call from a missing session.
        var result = await RunAsync(
            """{"id": "anon", "action": "list_sessions", "auth": "none"}""",
            """{"id": "bad", "action": "list_sessions", "auth": "bad"}""",
            """{"id": "missing", "action": "get_session", "session_id": "nobody"}""");

        Assert.Equal("completed", (string?)result["status"]);
        Assert.Equal(401, (int?)Fields(result, "anon")["status"]);
        Assert.Equal(401, (int?)Fields(result, "bad")["status"]);
        Assert.Equal(404, (int?)Fields(result, "missing")["status"]);
        Assert.False((bool?)Fields(result, "missing")["ok"]);
        // A refusal is an observation, not a driver fault.
        Assert.Null(Fields(result, "missing")["error"]);
        Assert.Equal("unknown session: nobody", (string?)Fields(result, "missing")["body"]!["detail"]);
    }

    [Fact]
    public async Task A_raw_post_creates_a_session_the_library_calls_then_read()
    {
        var id = "live" + Guid.NewGuid().ToString("N")[..8];
        var result = await RunAsync(
            $$$"""{"id": "create", "action": "http_post", "path": "/api/sessions", "body": {"session_id": "{{{id}}}"}}""",
            $$"""{"id": "read", "action": "get_session", "session_id": "{{id}}"}""",
            $$"""{"id": "snapshot", "action": "session_snapshot", "session_id": "{{id}}"}""",
            """{"id": "listed", "action": "list_sessions"}""");

        Assert.Equal("completed", (string?)result["status"]);
        Assert.True((bool?)Fields(result, "create")["ok"]);
        Assert.Equal(id, (string?)Fields(result, "read")["body"]!["session_id"]);
        Assert.Equal(200, (int?)Fields(result, "snapshot")["status"]);
        // The library's body, not the wire's — but the library hands the array
        // back as the server sent it, so the two agree. A port that wrapped it
        // would show up right here.
        var listed = Fields(result, "listed")["body"]!.AsArray();
        Assert.Contains(listed, s => (string?)s!["session_id"] == id);
    }

    [Fact]
    public async Task A_body_no_parser_accepts_is_recorded_as_non_json()
    {
        var result = await RunAsync(
            """{"id": "prom", "action": "http_get", "path": "/api/metrics/prometheus"}""",
            """{"id": "html", "action": "http_get", "path": "/app/"}""");

        // The bytes differ in every language; "no parser accepts this" does not.
        Assert.Equal(200, (int?)Fields(result, "prom")["status"]);
        Assert.Equal("<non-json>", (string?)Fields(result, "prom")["body"]);
        Assert.Equal("<non-json>", (string?)Fields(result, "html")["body"]);
    }

    [Fact]
    public async Task An_action_the_driver_does_not_know_is_an_error_not_a_skip()
    {
        var result = await RunAsync(
            """{"id": "health", "action": "health"}""",
            """{"id": "odd", "action": "teleport"}""");

        Assert.Equal("error", (string?)result["status"]);
        Assert.Contains("teleport", (string?)result["error"]!, StringComparison.Ordinal);
        // What did run is still reported: a silent skip is the one thing forbidden.
        Assert.Single(result["steps"]!.AsArray());
    }

    [Fact]
    public async Task A_scenario_needing_a_capability_this_driver_lacks_is_unsupported()
    {
        var result = await RunAsync(
            ["""{"id": "health", "action": "health"}"""],
            requires: """["rfb.raw"]""");

        Assert.Equal("unsupported", (string?)result["status"]);
        Assert.Contains("rfb.raw", (string?)result["error"]!, StringComparison.Ordinal);
        Assert.Empty(result["steps"]!.AsArray());
    }

    [Fact]
    public async Task A_step_the_scenario_left_incomplete_records_its_own_error()
    {
        var result = await RunAsync(
            """{"id": "no_id", "action": "get_session"}""",
            """{"id": "no_path", "action": "http_get"}""");

        // The run still completed: the driver did not break, the scenario did.
        Assert.Equal("completed", (string?)result["status"]);
        Assert.Null(Fields(result, "no_id")["status"]);
        Assert.False((bool?)Fields(result, "no_id")["ok"]);
        Assert.Contains("session_id", (string?)Fields(result, "no_id")["error"]!, StringComparison.Ordinal);
        Assert.Contains("path", (string?)Fields(result, "no_path")["error"]!, StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_server_that_never_answered_records_a_null_status()
    {
        var line = await ClientLineAsync(ClosedBaseUrl(), _server.Token, ScenarioFile("""{"id": "health", "action": "health"}"""));
        var result = JsonNode.Parse(line)!.AsObject();

        Assert.Equal("completed", (string?)result["status"]);
        var fields = Fields(result, "health");
        Assert.Null(fields["status"]);
        Assert.False((bool?)fields["ok"]);
        Assert.NotNull((string?)fields["error"]);
    }

    private static string ClosedBaseUrl()
    {
        // Take a port and give it straight back, so nothing is listening on it.
        var probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start();
        var port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        return $"http://{IPAddress.Loopback}:{port}";
    }

    private static JsonObject Fields(JsonObject result, string stepId)
    {
        var step = result["steps"]!.AsArray().Single(s => (string?)s!["id"] == stepId);
        return step!["fields"]!.AsObject();
    }

    private Task<JsonObject> RunAsync(params string[] steps) => RunAsync(steps, requires: null);

    private async Task<JsonObject> RunAsync(string[] steps, string? requires)
    {
        var path = ScenarioFile(steps, requires);
        var line = await ClientLineAsync(_server.BaseUrl, _server.Token, path);
        return JsonNode.Parse(line)!.AsObject();
    }

    private static async Task<string> ClientLineAsync(string baseUrl, string token, string scenarioPath)
    {
        var writer = new LiveLineWriter();
        try
        {
            var code = await LiveDriver.ExecuteAsync(
                ["client", "--base-url", baseUrl, "--token", token, "--scenario", scenarioPath],
                writer);

            // A driver that observed something exits 0; the observation is the result.
            Assert.Equal(0, code);
        }
        finally
        {
            File.Delete(scenarioPath);
        }

        return await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(60));
    }

    private static string ScenarioFile(params string[] steps) => ScenarioFile(steps, null);

    private static string ScenarioFile(string[] steps, string? requires)
    {
        var path = Path.Combine(Path.GetTempPath(), "live-scenario-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, $$"""
        {"id": "900_driver_test", "title": "Driver test",
         "requires": {{requires ?? "[]"}},
         "steps": [{{string.Join(",", steps)}}],
         "expect": []}
        """);
        return path;
    }
}

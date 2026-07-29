//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;
using Provide.Uterm.Conformance;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// How the `serve` role starts and — the part a harness depends on — how it stops.
/// A driver that outlives its stdin is a leaked server on someone's CI machine.
/// </summary>
public sealed class ConformanceLiveDriverServeTests : IDisposable
{
    private readonly string? _previousTokenPath;
    private readonly string _tokenPath;

    public ConformanceLiveDriverServeTests()
    {
        _previousTokenPath = Environment.GetEnvironmentVariable("UTERM_DEV_TOKEN_PATH");
        _tokenPath = Path.Combine(Path.GetTempPath(), "live-serve-token-" + Guid.NewGuid().ToString("N"));
        Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", _tokenPath);
    }

    public void Dispose()
    {
        Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", _previousTokenPath);
        File.Delete(_tokenPath);
    }

    [Fact]
    public async Task Closing_stdin_is_the_ordinary_shutdown()
    {
        var stdin = new LiveGatedStdin();
        var writer = new LiveLineWriter();
        var serving = LiveDriver.ExecuteAsync(["serve"], writer, input: stdin);
        var handshake = JsonNode.Parse(await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(60)))!.AsObject();
        var baseUrl = (string)handshake["base_url"]!;

        using (var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) })
        {
            var live = await http.GetAsync(baseUrl + "/api/health");
            Assert.True(live.IsSuccessStatusCode);
        }

        stdin.ReachEof();
        Assert.Equal(0, await serving.WaitAsync(TimeSpan.FromSeconds(60)));

        using var afterwards = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        await Assert.ThrowsAsync<HttpRequestException>(() => afterwards.GetAsync(baseUrl + "/api/health"));
    }

    [Fact]
    public async Task A_signalled_driver_stops_too()
    {
        var stdin = new LiveGatedStdin();
        var writer = new LiveLineWriter();
        using var signalled = new CancellationTokenSource();
        var serving = LiveDriver.ExecuteAsync(["serve"], writer, input: stdin, ct: signalled.Token);
        await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(60));

        await signalled.CancelAsync();

        Assert.Equal(0, await serving.WaitAsync(TimeSpan.FromSeconds(60)));
        stdin.ReachEof();
    }

    [Fact]
    public async Task The_scenario_names_the_auth_mode_when_no_flag_does()
    {
        var scenario = Path.Combine(Path.GetTempPath(), "live-auth-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(scenario, """
        {"id": "060_jwt", "title": "T", "auth": "jwt", "steps": [{"id": "s", "action": "health"}], "expect": []}
        """);
        var stdin = new LiveGatedStdin();
        var writer = new LiveLineWriter();
        try
        {
            var serving = LiveDriver.ExecuteAsync(["serve", "--scenario", scenario], writer, input: stdin);
            var handshake = JsonNode.Parse(await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(60)))!.AsObject();

            // Only the dev-token path mints one; a jwt server has no token to report.
            Assert.Equal("", (string?)handshake["token"]);
            stdin.ReachEof();
            await serving.WaitAsync(TimeSpan.FromSeconds(60));
        }
        finally
        {
            File.Delete(scenario);
        }
    }

    [Fact]
    public async Task A_scenario_that_cannot_be_read_stops_the_serve_role_before_it_binds()
    {
        var writer = new LiveLineWriter();
        var errors = new StringWriter();

        var code = await LiveDriver.ExecuteAsync(
            ["serve", "--scenario", Path.Combine(Path.GetTempPath(), "no-such-" + Guid.NewGuid().ToString("N"))],
            writer,
            errors,
            new MemoryStream());

        Assert.Equal(LiveDriver.UsageExitCode, code);
        Assert.Contains("cannot read scenario", errors.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void The_handshake_is_the_shape_the_protocol_prints()
    {
        var handshake = LiveServeDriver.Handshake(Unreachable, "tok");

        Assert.Equal(
            ["role", "language", "base_url", "token", "capabilities"],
            handshake.Select(kv => kv.Key));
        Assert.Equal("server", (string?)handshake["role"]);
        Assert.Equal("csharp", (string?)handshake["language"]);
        Assert.Equal(Unreachable, (string?)handshake["base_url"]);
    }

    /// <summary>A base URL nothing contacts — no port is named anywhere in this harness.</summary>
    private static string Unreachable => "http://" + System.Net.IPAddress.Loopback;

    [Fact]
    public async Task No_arguments_is_a_usage_error_and_help_is_not()
    {
        var usage = new StringWriter();
        Assert.Equal(LiveDriver.UsageExitCode, await LiveDriver.ExecuteAsync([], usage));
        Assert.Contains("uterm-live-driver serve", usage.ToString(), StringComparison.Ordinal);

        Assert.Equal(0, await LiveDriver.ExecuteAsync(["--help"], new StringWriter()));
    }

    [Fact]
    public async Task A_role_the_driver_does_not_have_is_refused()
    {
        var errors = new StringWriter();

        Assert.Equal(LiveDriver.UsageExitCode, await LiveDriver.ExecuteAsync(["judge"], new StringWriter(), errors));
        Assert.Contains("unknown role judge", errors.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task The_client_role_needs_a_base_url_and_a_scenario()
    {
        var errors = new StringWriter();

        var code = await LiveDriver.ExecuteAsync(["client", "--token", "t"], new StringWriter(), errors);

        Assert.Equal(LiveDriver.UsageExitCode, code);
        Assert.Contains("--base-url", errors.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task A_driver_that_broke_still_reports_a_cell()
    {
        // A missing scenario is the driver's fault, not the server's. Dying here
        // would leave the harness with a hole instead of a reason.
        var writer = new LiveLineWriter();

        var code = await LiveDriver.ExecuteAsync(
            ["client", "--base-url", Unreachable, "--token", "t", "--scenario", "070_absent.json"],
            writer);

        Assert.Equal(0, code);
        var result = JsonNode.Parse(await writer.FirstLine.WaitAsync(TimeSpan.FromSeconds(30)))!.AsObject();
        Assert.Equal("error", (string?)result["status"]);
        Assert.Equal("070_absent", (string?)result["scenario_id"]);
        Assert.Contains("cannot read scenario", (string?)result["error"]!, StringComparison.Ordinal);
        Assert.Empty(result["steps"]!.AsArray());
    }
}

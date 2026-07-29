//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;

namespace Provide.Uterm.Conformance;

/// <summary>
/// Entry point for the C# live conformance driver (`conformance/live/PROTOCOL.md`).
///
/// One executable, two roles: `serve` hosts the real server on an ephemeral
/// port, `client` runs a scenario against one. Neither judges anything — the
/// harness evaluates every expectation, in one implementation, so four
/// languages cannot disagree about what an expectation means.
/// </summary>
public static class LiveDriver
{
    /// <summary>
    /// What this driver has, in the vocabulary scenarios use to require things
    /// (the same names the Go driver reports):
    ///
    /// <list type="bullet">
    /// <item><c>hijack.rest</c> — the acquire/send/step/release REST surface of HijackClient.</item>
    /// <item><c>sessions.rest</c> — session list/get/snapshot through the client library.</item>
    /// <item><c>http.raw</c> — arbitrary <c>http_get</c>/<c>http_post</c> steps.</item>
    /// <item><c>auth.dev_token</c> — <c>serve --auth dev_token</c> mints a presentable bearer.</item>
    /// <item><c>status.observed</c> — the status a client-library call returned is recorded
    /// rather than collapsed into the library's <c>(ok, body)</c>, because
    /// <see cref="Client.HijackClient"/> takes an injected <see cref="HttpClient"/>.</item>
    /// </list>
    /// </summary>
    public static readonly IReadOnlyList<string> Capabilities =
    [
        "hijack.rest",
        "sessions.rest",
        "http.raw",
        "auth.dev_token",
        "status.observed",
    ];

    public const int UsageExitCode = 2;

    /// <summary>Run one role. Writers and stdin are injected so tests drive both.</summary>
    public static async Task<int> ExecuteAsync(
        string[] args,
        TextWriter? output = null,
        TextWriter? error = null,
        Stream? input = null,
        CancellationToken ct = default)
    {
        output ??= Console.Out;
        error ??= Console.Error;

        if (args.Length == 0 || args[0] is "-h" or "--help" or "help")
        {
            WriteUsage(output);
            return args.Length == 0 ? UsageExitCode : 0;
        }

        var flags = Root.ParseFlags(args.Skip(1).ToArray());
        return args[0] switch
        {
            "serve" => await ServeAsync(flags, output, error, input, ct).ConfigureAwait(false),
            "client" => await ClientAsync(flags, output, error, ct).ConfigureAwait(false),
            _ => Unknown(args[0], error),
        };
    }

    private static int Unknown(string command, TextWriter error)
    {
        error.WriteLine($"error: unknown role {command} (expected serve or client)");
        return UsageExitCode;
    }

    private static void WriteUsage(TextWriter w)
    {
        w.WriteLine("uterm-live-driver — C# driver for the live conformance harness.");
        w.WriteLine();
        w.WriteLine("Usage:");
        w.WriteLine("  uterm-live-driver serve [--auth MODE] [--scenario FILE]");
        w.WriteLine("  uterm-live-driver client --base-url URL --token TOKEN --scenario FILE");
        w.WriteLine();
        w.WriteLine("Capabilities: " + string.Join(", ", Capabilities));
    }

    private static async Task<int> ServeAsync(
        IReadOnlyDictionary<string, string> flags,
        TextWriter output,
        TextWriter error,
        Stream? input,
        CancellationToken ct)
    {
        var auth = flags.GetValueOrDefault("auth", "");
        if (string.IsNullOrEmpty(auth) && flags.TryGetValue("scenario", out var scenarioPath))
        {
            // The scenario names the mode the server is asked to start in.
            try
            {
                auth = LiveScenario.Load(scenarioPath).Auth;
            }
            catch (Exception ex)
            {
                error.WriteLine("error: " + Describe(ex));
                return UsageExitCode;
            }
        }

        var options = new LiveServeOptions
        {
            Auth = string.IsNullOrEmpty(auth) ? LiveScenario.DefaultServerAuth : auth,
            Version = Root.Version,
        };
        return await LiveServeDriver
            .ServeAsync(options, output, input ?? Console.OpenStandardInput(), ct)
            .ConfigureAwait(false);
    }

    private static async Task<int> ClientAsync(
        IReadOnlyDictionary<string, string> flags, TextWriter output, TextWriter error, CancellationToken ct)
    {
        var baseUrl = flags.GetValueOrDefault("base-url", "");
        var scenarioPath = flags.GetValueOrDefault("scenario", "");
        if (string.IsNullOrEmpty(baseUrl) || string.IsNullOrEmpty(scenarioPath))
        {
            error.WriteLine("error: client requires --base-url URL and --scenario FILE");
            return UsageExitCode;
        }

        var token = flags.GetValueOrDefault("token", "");
        LiveResult result;
        try
        {
            var scenario = LiveScenario.Load(scenarioPath);
            result = await LiveClientDriver.RunAsync(scenario, baseUrl, token, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // The driver broke, not the server. Say so in the result rather than
            // dying, so the harness records a cell instead of a missing one.
            result = new LiveResult
            {
                ScenarioId = System.IO.Path.GetFileNameWithoutExtension(scenarioPath),
                Role = LiveResult.RoleClient,
                Status = LiveResult.StatusError,
                Capabilities = Capabilities,
                Steps = [],
                Error = Describe(ex),
            };
        }

        output.WriteLine(result.ToJsonLine());
        await output.FlushAsync(ct).ConfigureAwait(false);
        return 0;
    }

    private static string Describe(Exception ex) =>
        ex is LiveDriverException ? ex.Message : ex.GetType().Name + ": " + ex.Message;
}

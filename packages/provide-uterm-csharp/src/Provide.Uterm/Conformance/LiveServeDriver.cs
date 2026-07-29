//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text.Json.Nodes;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Conformance;

/// <summary>Options for the driver's `serve` role.</summary>
public sealed class LiveServeOptions
{
    /// <summary>Auth mode the server starts in. Only <c>dev_token</c> mints a token.</summary>
    public string Auth { get; init; } = LiveScenario.DefaultServerAuth;

    /// <summary>Server version string reported by /api/health.</summary>
    public string Version { get; init; } = "0.0.0-dev";
}

/// <summary>
/// The `serve` role: bind the real <see cref="UtermServer"/> on an ephemeral
/// port, say where it landed, and keep serving until stdin closes.
///
/// The port is the operating system's to choose — nothing here may name one,
/// or two drivers on one machine would collide and the harness could not say why.
/// </summary>
public static class LiveServeDriver
{
    /// <summary>
    /// Start the server, write the handshake line, and serve until
    /// <paramref name="input"/> reaches EOF or <paramref name="ct"/> is cancelled.
    /// </summary>
    public static async Task<int> ServeAsync(
        LiveServeOptions options, TextWriter output, Stream input, CancellationToken ct = default)
    {
        var host = IPAddress.Loopback.ToString();
        var config = UtermServerConfig.Default();
        config.Server.Host = host;
        // 0 is not a port: it is the ask for whichever one is free.
        config.Server.Port = 0;
        config.Server.PublicBaseUrl = "";
        config.Auth.Mode = options.Auth;

        var (server, devToken) = ServerFactory.CreateFromConfig(config, options.Version);
        await using (server)
        {
            server.Build([$"http://{host}:0"]);
            await server.StartAsync(ct).ConfigureAwait(false);
            var baseUrl = (server.BaseAddress ?? "").TrimEnd('/');
            // Links the server hands out must name the port it actually got.
            config.Server.PublicBaseUrl = baseUrl;

            output.WriteLine(Handshake(baseUrl, devToken ?? "").ToJsonString());
            await output.FlushAsync(ct).ConfigureAwait(false);

            await WaitForShutdownAsync(input, ct).ConfigureAwait(false);
            await server.StopAsync(CancellationToken.None).ConfigureAwait(false);
        }

        return 0;
    }

    /// <summary>The one line of JSON a server driver writes to stdout.</summary>
    public static JsonObject Handshake(string baseUrl, string token)
    {
        var capabilities = new JsonArray();
        foreach (var capability in LiveDriver.Capabilities)
        {
            capabilities.Add(capability);
        }

        return new JsonObject
        {
            ["role"] = LiveResult.RoleServer,
            ["language"] = LiveResult.LanguageName,
            ["base_url"] = baseUrl,
            ["token"] = token,
            ["capabilities"] = capabilities,
        };
    }

    /// <summary>
    /// Ordinary shutdown is stdin reaching EOF. The read runs off the caller's
    /// thread because a console stdin read ignores cancellation, and a signalled
    /// driver still has to stop.
    /// </summary>
    private static async Task WaitForShutdownAsync(Stream input, CancellationToken ct)
    {
        var cancelled = new TaskCompletionSource();
        await using var registration = ct.Register(() => cancelled.TrySetResult()).ConfigureAwait(false);
        var eof = Task.Run(() => DrainToEof(input), CancellationToken.None);
        await Task.WhenAny(eof, cancelled.Task).ConfigureAwait(false);
    }

    /// <summary>Block until stdin is done. A pipe that broke is the same shutdown as EOF.</summary>
    internal static void DrainToEof(Stream input)
    {
        var buffer = new byte[64];
        try
        {
            while (input.Read(buffer, 0, buffer.Length) > 0)
            {
                // The harness sends nothing; anything that arrives is discarded.
            }
        }
        catch (IOException)
        {
            // A closed pipe is the same shutdown as EOF.
        }
        catch (ObjectDisposedException)
        {
            // Same: stdin went away.
        }
    }
}

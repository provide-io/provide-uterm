//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// Two security postures an operator can write down, and the hosted server has
/// to honour both.
///
/// Each was decided in the factory instead of read from the file:
///
/// 1. <c>api_keys_enabled = false</c> could not take effect.
///    <c>ServerFactory.CreateFromConfig</c> contained
///    <c>if (!cfg.Auth.ApiKeysEnabled) { cfg.Auth.ApiKeysEnabled = true; }</c>,
///    commented "tests can disable" — inverted, since it is exactly production
///    that cannot. The reference defaults this to <c>False</c>
///    (<c>config_schema.py:72</c>), so the port both ignored the operator and
///    diverged on the default.
///
/// 2. <c>webhooks.allow_loopback_destinations</c> did not exist here, and the
///    factory hardcoded <c>allowLoopbackDestinations: true</c>. The reference
///    reads it from config and defaults it to <c>False</c>
///    (<c>config_schema.py:319</c>, <c>factory_impl.py:366</c>), and so does
///    <c>WebhookManager.__init__</c>. This port shipped the SSRF guard
///    permanently off with no key to switch it on — a webhook pointed at
///    <c>127.0.0.1</c> reaches whatever else is listening on the box.
///
/// Both are asserted over real HTTP against a server built by the production
/// factory, because that is the only layer at which either bug was visible: the
/// config object was correct in both cases right up until the factory overrode
/// it.
/// </summary>
public sealed class HostedConfigSecurityDefaultsTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string WriteToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-hosted-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, body);
        return path;
    }

    /// <summary>Boot through the production factory from an actual TOML file.</summary>
    /// <param name="bindHost">
    /// What <c>[server] host</c> says. It is not the address this listens on —
    /// the URL handed to <see cref="UtermServer.Build"/> is always loopback — so
    /// a routable-bind case can be exercised without opening a public port. The
    /// distinction matters because the bind address is half of the webhook
    /// egress guard's effective loopback permission
    /// (<c>conformance/EGRESS_GUARD.md</c> §3).
    /// </param>
    private static async Task<(UtermServer Server, HttpClient Client, string Token)> BootFromTomlAsync(
        string body,
        string bindHost = "127.0.0.1")
    {
        var port = FreePort();
        var path = WriteToml(body);
        UtermServerConfig cfg;
        try
        {
            cfg = ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }

        cfg.Server.Host = bindHost;
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });

        var (server, _) = ServerFactory.CreateFromConfig(cfg, "hosted-security");
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hs-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });

        var client = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}") };
        client.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
        return (server, client, token);
    }

    [Fact]
    public async Task ApiKeyManagementStaysOffWhenTheOperatorSaysSo()
    {
        var (server, client, _) = await BootFromTomlAsync("[auth]\napi_keys_enabled = false\n");
        await using (server)
        {
            using (client)
            {
                var response = await client.GetAsync("/api/keys");

                // 403 with the route's own message, not 200 with a key list.
                Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
                Assert.Contains("disabled", await response.Content.ReadAsStringAsync(), StringComparison.Ordinal);
            }
        }
    }

    [Fact]
    public async Task ApiKeyManagementIsOffByDefaultLikeTheReference()
    {
        // The reference default is False. A port that silently enables an auth
        // surface the reference leaves closed is a divergence in the direction
        // that matters.
        var (server, client, _) = await BootFromTomlAsync("");
        await using (server)
        {
            using (client)
            {
                Assert.Equal(HttpStatusCode.Forbidden, (await client.GetAsync("/api/keys")).StatusCode);
            }
        }
    }

    [Fact]
    public async Task ApiKeyManagementTurnsOnWhenAskedFor()
    {
        // The guard against fixing the override by simply refusing always.
        var (server, client, _) = await BootFromTomlAsync("[auth]\napi_keys_enabled = true\n");
        await using (server)
        {
            using (client)
            {
                Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/api/keys")).StatusCode);
            }
        }
    }

    /// <summary>Register a webhook aimed at loopback, as an operator would.</summary>
    /// <remarks>
    /// A measured <see cref="StringContent"/> rather than <c>PostAsJsonAsync</c>:
    /// the latter streams without a Content-Length, and <c>ReadJson</c>
    /// (UtermServer.cs:1365) treats a null length as an empty body — so the
    /// request arrived as <c>{}</c> and every assertion here was really about
    /// "url is required". That is a server-side bug in its own right, tracked
    /// separately; this test must not depend on it either way.
    /// </remarks>
    private static Task<HttpResponseMessage> RegisterLoopbackWebhook(HttpClient client) =>
        client.PostAsync(
            "/api/sessions/demo/webhooks",
            new StringContent(
                """{"url":"http://127.0.0.1:9/hook","event_types":["session.started"]}""",
                System.Text.Encoding.UTF8,
                "application/json"));

    [Fact]
    public async Task ALoopbackWebhookIsRefusedByDefaultOnARoutableBind()
    {
        // The bind is routable, so the config key is the only thing that could
        // permit a loopback destination — and unset, it does not.
        //
        // This assertion used to be made on the default loopback bind, which was
        // the wrong posture: §3 makes a loopback bind *itself* permit loopback
        // destinations, because refusing them there protects nothing (no remote
        // caller can reach the listener) while breaking every single-box
        // deployment. The whole matrix lives in WebhookEgressGuardTests; what
        // this file is still about is the config key not being overridden by the
        // factory.
        var (server, client, _) = await BootFromTomlAsync("", bindHost: "0.0.0.0");
        await using (server)
        {
            using (client)
            {
                var response = await RegisterLoopbackWebhook(client);
                var body = await response.Content.ReadAsStringAsync();

                // Asserted on the guard's own message, not merely on "not 200".
                // A first draft of this test checked `NotEqual(OK)` and passed
                // against the unfixed server — the request was failing on a
                // malformed payload, so the assertion said nothing at all. Any
                // refusal reason satisfies "not OK"; only one of them is this one.
                Assert.Equal(HttpStatusCode.UnprocessableEntity, response.StatusCode);
                Assert.Contains("loopback", body, StringComparison.OrdinalIgnoreCase);
            }
        }
    }

    [Fact]
    public async Task ALoopbackWebhookIsAllowedWhenTheOperatorOptsIn()
    {
        // Local development and single-box deployments are the reason the key
        // exists; refusing regardless would be its own bug, and this is what
        // separates a wired guard from a stuck one.
        var (server, client, _) = await BootFromTomlAsync(
            "[webhooks]\nallow_loopback_destinations = true\n",
            bindHost: "0.0.0.0");
        await using (server)
        {
            using (client)
            {
                var response = await RegisterLoopbackWebhook(client);

                Assert.Equal(HttpStatusCode.OK, response.StatusCode);
                Assert.Contains("webhook_id", await response.Content.ReadAsStringAsync(), StringComparison.Ordinal);
            }
        }
    }
}

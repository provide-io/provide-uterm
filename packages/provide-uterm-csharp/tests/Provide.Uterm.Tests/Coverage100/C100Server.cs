//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Sockets;
using System.Security.Claims;
using System.Text;
using System.Text.Json;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// A loopback <see cref="UtermServer"/> in dev-token mode for the Coverage100
/// suites, with tokens mintable for any subject/roles/tenant against the same
/// signing secret, and the optional dependencies the routes under test need.
/// </summary>
internal sealed class C100Server : IAsyncDisposable
{
    public const string Session = "demo";

    private C100Server(UtermServer server, TermHub hub, UtermServerConfig cfg, string baseUrl, ServerDeps deps)
    {
        Server = server;
        Hub = hub;
        Config = cfg;
        BaseUrl = baseUrl;
        Deps = deps;
    }

    public UtermServer Server { get; }
    public TermHub Hub { get; }
    public UtermServerConfig Config { get; }
    public string BaseUrl { get; }
    public ServerDeps Deps { get; }
    public string AdminToken => Token("admin", "admin");

    public sealed class Options
    {
        public Action<UtermServerConfig>? Configure { get; init; }
        public Func<UtermServerConfig, TermHub, ServerDeps>? Deps { get; init; }
        public IClock? Clock { get; init; }

        /// <summary>False to leave the app built but not started (CreateHandler starts it).</summary>
        public bool Start { get; init; } = true;
    }

    public static async Task<C100Server> StartAsync(Options? options = null)
    {
        options ??= new Options();
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Auth.ApiKeysEnabled = true;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = Session,
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });
        options.Configure?.Invoke(cfg);
        var tokenPath = Path.Combine(Path.GetTempPath(), "c100-" + Guid.NewGuid().ToString("N"));
        DevIdp.Setup(cfg.Auth, new DevIdp.Options { TokenPath = tokenPath, Subject = "admin", Roles = new[] { "admin" } });
        File.Delete(tokenPath);

        var hub = new TermHub(new TermHubConfig
        {
            Clock = options.Clock ?? new RealClock(),
            WorkerToken = cfg.Auth.WorkerBearerToken,
            RestAcquireRateLimitPerSec = 1000,
            RestSendRateLimitPerSec = 1000,
        });
        var deps = options.Deps?.Invoke(cfg, hub) ?? Default(cfg, hub);
        var server = new UtermServer(deps);
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        if (options.Start)
        {
            await server.StartAsync();
        }

        return new C100Server(server, hub, cfg, $"http://127.0.0.1:{port}", deps);
    }

    public static ServerDeps Default(UtermServerConfig cfg, TermHub hub, ApiKeyStore? keys = null)
    {
        keys ??= new ApiKeyStore();
        return new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, keys),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            ApiKeys = keys,
            Profiles = new InMemoryProfileStore(),
            Version = "c100",
        };
    }

    /// <summary>A JWT for <paramref name="subject"/> signed with this server's dev secret.</summary>
    public string Token(string subject, params string[] roles) => TokenFor(subject, null, roles);

    public string TokenFor(string subject, string? tenant, params string[] roles) =>
        TokenWith(subject, tenant, roles, scopes: null);

    /// <summary>As <see cref="TokenFor"/>, optionally narrowed to <paramref name="scopes"/>.</summary>
    public string TokenWith(string subject, string? tenant, string[] roles, string? scopes)
    {
        var auth = Config.Auth;
        var claims = new List<Claim> { new("sub", subject) };
        if (scopes is not null)
        {
            claims.Add(new Claim(auth.JwtScopesClaim, scopes));
        }

        claims.AddRange(roles.Select(r => new Claim(auth.JwtRolesClaim, r)));
        if (tenant is not null)
        {
            claims.Add(new Claim(auth.JWTTenantClaim, tenant));
        }

        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(auth.JwtPublicKeyPem!));
        var jwt = new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: DateTime.UtcNow.AddMinutes(-1),
            expires: DateTime.UtcNow.AddHours(1),
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.HmacSha256));
        return new JwtSecurityTokenHandler().WriteToken(jwt);
    }

    public HttpClient Http(string? token = null)
    {
        var http = new HttpClient { BaseAddress = new Uri(BaseUrl) };
        if (token is not null)
        {
            http.DefaultRequestHeaders.Add("Authorization", "Bearer " + token);
        }

        return http;
    }

    public static StringContent Json(object body) =>
        new(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");

    public static async Task<JsonElement> ReadJsonAsync(HttpResponseMessage resp) =>
        JsonDocument.Parse(await resp.Content.ReadAsStringAsync()).RootElement.Clone();

    public static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    public ValueTask DisposeAsync() => Server.DisposeAsync();
}

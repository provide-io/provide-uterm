//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>JWT, JWKS, webhook-authz and config-coercion arms.</summary>
public sealed class Coverage100AuthConfigTests
{
    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(respond(request));
    }

    private static UtermServerConfig LoadToml(string toml)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-c100-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, toml);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    private static (LocalIdentityProvider Idp, string Token) DevJwt(Action<AuthConfig> configure, DevIdp.Options options)
    {
        var auth = UtermServerConfig.Default().Auth;
        configure(auth);
        options.TokenPath = Path.Combine(Path.GetTempPath(), "c100-jwt-" + Guid.NewGuid().ToString("N"));
        try
        {
            var token = DevIdp.Setup(auth, options);
            return (new LocalIdentityProvider(auth), token);
        }
        finally
        {
            File.Delete(options.TokenPath);
        }
    }

    [Fact]
    public void Jwt_with_a_blank_subject_is_refused()
    {
        var (idp, token) = DevJwt(_ => { }, new DevIdp.Options { Subject = "   " });
        var ex = Assert.Throws<SecurityTokenException>(() => idp.PrincipalFromJwtToken(token));
        Assert.Contains("sub claim is required", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Tenant_claim_renamed_by_inbound_mapping_is_read_from_the_payload()
    {
        // "tid" is in the handler's inbound claim map, so the principal carries
        // it under its long URI name and only the raw payload has "tid".
        var (idp, token) = DevJwt(a => a.JWTTenantClaim = "tid", new DevIdp.Options { Tenant = "acme" });
        Assert.Equal("acme", idp.PrincipalFromJwtToken(token).TenantId);
    }

    [Fact]
    public void Malformed_rsa_pem_is_reported_as_a_token_error()
    {
        var (_, token) = DevJwt(_ => { }, new DevIdp.Options());
        var auth = UtermServerConfig.Default().Auth;
        auth.Mode = "jwt";
        auth.JwtPublicKeyPem = "-----BEGIN PUBLIC KEY-----\nbm90IGEga2V5\n-----END PUBLIC KEY-----\n";
        auth.JwtAlgorithms = new List<string> { "HS256", "RS256" };
        var idp = new LocalIdentityProvider(auth);
        var ex = Assert.ThrowsAny<Exception>(() => idp.PrincipalFromJwtToken(token));
        Assert.Contains("jwt_public_key_pem", ex.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void Roles_fall_back_to_the_known_default_set_without_a_default_role()
    {
        var auth = UtermServerConfig.Default().Auth;
        auth.JwtDefaultRole = "";
        var roles = new LocalIdentityProvider(auth).RolesFromClaimList(new[] { "nonsense" });
        Assert.Equal(AuthRoles.FilterKnownRoles(Array.Empty<string>()).ToList(), roles.ToList());
    }

    [Fact]
    public void Jwks_failures_surface_as_token_errors_or_transport_errors()
    {
        Jwks.ResetCache();
        try
        {
            using var empty = new HttpClient(new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"keys":[]}""", Encoding.UTF8, "application/json"),
            }));
            Jwks.SetHttpClient(empty);
            var none = Assert.Throws<SecurityTokenException>(() => Jwks.ResolveKey("https://jwks.test/empty", null));
            Assert.Contains("no usable RSA keys", none.Message, StringComparison.Ordinal);

            Assert.Throws<SecurityTokenException>(() => Jwks.RsaFromJwk(new Jwks.JwkKey { Kty = "RSA", N = "", E = "AQAB" }));
            // A zero-length modulus decodes but cannot be imported.
            Assert.ThrowsAny<Exception>(() => Jwks.RsaFromJwk(new Jwks.JwkKey { Kty = "RSA", N = "AA", E = "" + "AQAB" }));

            // Default client (restored by passing null) against a closed port.
            Jwks.SetHttpClient(null);
            Assert.ThrowsAny<Exception>(() => Jwks.ResolveKey($"http://127.0.0.1:{ClosedPort()}/jwks", "k"));
        }
        finally
        {
            Jwks.SetHttpClient(null);
            Jwks.ResetCache();
        }
    }

    [Fact]
    public void Webhook_authz_denies_on_null_principals_refusals_missing_fields_and_oversized_bodies()
    {
        var mode = "500";
        using var http = new HttpClient(new StubHandler(_ => mode switch
        {
            "500" => new HttpResponseMessage(HttpStatusCode.InternalServerError),
            "empty" => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{}") },
            _ => new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(new byte[(1 << 20) + 10]) },
        }));
        using var authz = new WebhookAuthorizationProvider("https://authz.test/check", secret: "", httpClient: http);
        var p = new Principal { SubjectId = "u" };
        var session = new SessionDefinition { SessionId = "s" };

        Assert.Empty(authz.CapabilitiesFor(null!));
        Assert.Equal("viewer", authz.ResolveBrowserRole(null!, session));
        Assert.False(authz.HasCapability(null!, "x"));

        Assert.Empty(authz.CapabilitiesFor(p)); // non-200
        Assert.Equal("viewer", authz.ResolveBrowserRole(p, session));

        mode = "empty";
        Assert.Empty(authz.CapabilitiesFor(p)); // no "capabilities" field

        mode = "huge";
        Assert.False(authz.HasCapability(p, "x")); // body over 1 MiB
    }

    [Fact]
    public void Local_authz_mutation_needs_an_owner_and_service_exposes_capabilities()
    {
        var local = new LocalAuthorizationProvider();
        var op = new Principal { SubjectId = "alice", Roles = StringSet.Of("operator") };
        Assert.False(local.CanMutateSession(op, new SessionDefinition { SessionId = "s", Owner = null }, "session.control.update"));
        Assert.True(local.CanMutateSession(op, new SessionDefinition { SessionId = "s", Owner = "alice" }, "session.control.update"));
        Assert.Contains("session.read", new AuthorizationService().CapabilitiesFor(op));
    }

    [Fact]
    public void Webhook_signature_of_the_wrong_length_is_rejected()
    {
        var body = "{}"u8.ToArray();
        var ts = WebhookSigning.FormatTimestamp(1000);
        Assert.False(WebhookSigning.VerifyWebhookSignature("secret", body, "sha256=abc", ts, now: 1000));
    }

    [Fact]
    public void Config_numbers_accept_integers_strings_and_fall_back_on_other_types()
    {
        var cfg = LoadToml("""
            [auth]
            webhook_idp_timeout_s = 3

            [governance]
            authz_webhook_timeout_s = "2.5"
            policy_webhook_timeout_s = true

            [[sessions]]
            session_id = "a"
            auto_transfer_idle_s = 7.9

            [[sessions]]
            session_id = "b"
            auto_transfer_idle_s = "x"
            """);
        Assert.Equal(3.0, cfg.Auth.WebhookIdpTimeoutS);
        Assert.Equal(2.5, cfg.Governance.AuthzWebhookTimeoutS);
        Assert.Equal(UtermServerConfig.Default().Governance.PolicyWebhookTimeoutS, cfg.Governance.PolicyWebhookTimeoutS);
        Assert.Equal(7, cfg.Sessions.Single(s => s.SessionId == "a").AutoTransferIdleS);
        Assert.Equal(30, cfg.Sessions.Single(s => s.SessionId == "b").AutoTransferIdleS);
    }

    private static int ClosedPort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}

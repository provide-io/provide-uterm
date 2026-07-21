//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.ServerAuth;

/// <summary>
/// Production JWT path: JWKS + RS256 (CF Access), RSA public PEM, and DevIdp HS256.
/// Mirrors Go serverauth/keys_test.go patterns.
/// </summary>
public class JwksJwtTests : IDisposable
{
    public JwksJwtTests()
    {
        Jwks.ResetCache();
        Jwks.SetHttpClient(null);
    }

    public void Dispose()
    {
        Jwks.ResetCache();
        Jwks.SetHttpClient(null);
    }

    private static AuthConfig JwtBase(params string[] algorithms) => new()
    {
        Mode = "jwt",
        JwtIssuer = "provide-uterm",
        JwtAudience = "provide-uterm-server",
        JwtAlgorithms = algorithms.ToList(),
        JwtRolesClaim = "roles",
        ClockSkewSeconds = 15,
    };

    private static string SignRs256(RSA privateKey, string? kid, string sub = "rsa-user", string[]? roles = null)
    {
        roles ??= new[] { "admin" };
        var now = DateTimeOffset.UtcNow;
        var claims = new List<Claim> { new("sub", sub) };
        foreach (var r in roles)
        {
            claims.Add(new Claim("roles", r));
        }

        var key = new RsaSecurityKey(privateKey);
        if (!string.IsNullOrEmpty(kid))
        {
            key.KeyId = kid;
        }

        var token = new JwtSecurityToken(
            issuer: "provide-uterm",
            audience: "provide-uterm-server",
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddSeconds(600).UtcDateTime,
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.RsaSha256));
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    private static string SignHs256(string secret, string sub = "hs-user", string[]? roles = null)
    {
        roles ??= new[] { "viewer" };
        var now = DateTimeOffset.UtcNow;
        var claims = new List<Claim> { new("sub", sub) };
        foreach (var r in roles)
        {
            claims.Add(new Claim("roles", r));
        }

        // IdentityModel requires HS256 keys ≥ 256 bits.
        var keyBytes = Encoding.UTF8.GetBytes(secret);
        if (keyBytes.Length < 32)
        {
            var padded = new byte[32];
            Array.Copy(keyBytes, padded, keyBytes.Length);
            keyBytes = padded;
        }

        var key = new SymmetricSecurityKey(keyBytes);
        var token = new JwtSecurityToken(
            issuer: "provide-uterm",
            audience: "provide-uterm-server",
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddSeconds(600).UtcDateTime,
            signingCredentials: new SigningCredentials(key, SecurityAlgorithms.HmacSha256));
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    private static string RsaPublicPem(RSA key)
    {
        var pub = key.ExportSubjectPublicKeyInfo();
        var b64 = Convert.ToBase64String(pub);
        var sb = new StringBuilder();
        sb.AppendLine("-----BEGIN PUBLIC KEY-----");
        for (var i = 0; i < b64.Length; i += 64)
        {
            sb.AppendLine(b64.Substring(i, Math.Min(64, b64.Length - i)));
        }

        sb.AppendLine("-----END PUBLIC KEY-----");
        return sb.ToString();
    }

    private static string BuildJwksJson(RSA publicKey, string? kid)
    {
        var rsaParams = publicKey.ExportParameters(false);
        var n = Base64UrlEncoder.Encode(rsaParams.Modulus!);
        var e = Base64UrlEncoder.Encode(rsaParams.Exponent!);
        var keyObj = new Dictionary<string, object?>
        {
            ["kty"] = "RSA",
            ["n"] = n,
            ["e"] = e,
        };
        if (kid is not null)
        {
            keyObj["kid"] = kid;
        }

        return JsonSerializer.Serialize(new { keys = new[] { keyObj } });
    }

    private static string BuildMultiJwksJson(RSA key1, string kid1, RSA key2, string kid2)
    {
        static Dictionary<string, object?> Entry(RSA key, string kid)
        {
            var rsaParams = key.ExportParameters(false);
            return new Dictionary<string, object?>
            {
                ["kty"] = "RSA",
                ["kid"] = kid,
                ["n"] = Base64UrlEncoder.Encode(rsaParams.Modulus!),
                ["e"] = Base64UrlEncoder.Encode(rsaParams.Exponent!),
            };
        }

        return JsonSerializer.Serialize(new { keys = new[] { Entry(key1, kid1), Entry(key2, kid2) } });
    }

    private static HttpClient StubHttpClient(Func<HttpRequestMessage, HttpResponseMessage> handler)
    {
        return new HttpClient(new StubHandler(handler)) { Timeout = TimeSpan.FromSeconds(10) };
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, HttpResponseMessage> _handler;

        public StubHandler(Func<HttpRequestMessage, HttpResponseMessage> handler) => _handler = handler;

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(_handler(request));
    }

    [Fact]
    public void Rs256_Via_Jwks_HappyPath_With_Kid()
    {
        using var rsa = RSA.Create(2048);
        const string kid = "kid-1";
        const string url = "https://jwks.test/certs-happy";
        var doc = BuildJwksJson(rsa, kid);
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);

        var token = SignRs256(rsa, kid);
        var p = idp.PrincipalFromJwtToken(token);
        Assert.Equal("rsa-user", p.SubjectId);
        Assert.True(p.Roles.Has("admin"));

        // Second call hits the process-wide cache.
        var p2 = idp.PrincipalFromJwtToken(SignRs256(rsa, kid, sub: "rsa-user-2"));
        Assert.Equal("rsa-user-2", p2.SubjectId);
    }

    [Fact]
    public void SingleKey_Jwks_Empty_Kid_Accepts()
    {
        using var rsa = RSA.Create(2048);
        const string url = "https://jwks.test/certs-single";
        // Document has a kid, token has none — single-key fallback accepts.
        var doc = BuildJwksJson(rsa, "only-key");
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);

        var token = SignRs256(rsa, kid: null);
        var p = idp.PrincipalFromJwtToken(token);
        Assert.Equal("rsa-user", p.SubjectId);
    }

    [Fact]
    public async Task Unknown_Kid_MultiKey_Fails_AuthenticateAsync_Anonymous()
    {
        using var rsa1 = RSA.Create(2048);
        using var rsa2 = RSA.Create(2048);
        const string url = "https://jwks.test/certs-multi";
        var doc = BuildMultiJwksJson(rsa1, "a", rsa2, "b");
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);

        var token = SignRs256(rsa1, kid: "unknown-kid");
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + token,
            },
        });
        Assert.Equal("anonymous", p.SubjectId);
        Assert.True(p.Roles.Has("viewer"));
    }

    [Fact]
    public async Task Jwks_Http_Error_Fails_Closed()
    {
        using var rsa = RSA.Create(2048);
        const string url = "https://jwks.test/certs-500";
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.InternalServerError)));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);

        var token = SignRs256(rsa, "k");
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + token,
            },
        });
        Assert.Equal("anonymous", p.SubjectId);

        // Direct call surfaces the failure.
        Assert.ThrowsAny<Exception>(() => idp.PrincipalFromJwtToken(token));
    }

    [Fact]
    public void Jwks_No_Rsa_Keys_Fails_Closed()
    {
        using var rsa = RSA.Create(2048);
        const string url = "https://jwks.test/certs-oct";
        var doc = JsonSerializer.Serialize(new
        {
            keys = new[] { new { kty = "oct", kid = "x", k = "YWJj" } },
        });
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);
        Assert.ThrowsAny<Exception>(() => idp.PrincipalFromJwtToken(SignRs256(rsa, "x")));
    }

    [Fact]
    public void Rs256_Via_Rsa_Public_Pem()
    {
        using var rsa = RSA.Create(2048);
        var pem = RsaPublicPem(rsa);
        var cfg = JwtBase("RS256");
        cfg.JwtPublicKeyPem = pem;
        var idp = new LocalIdentityProvider(cfg);

        var p = idp.PrincipalFromJwtToken(SignRs256(rsa, kid: null));
        Assert.Equal("rsa-user", p.SubjectId);
        Assert.True(p.Roles.Has("admin"));
    }

    [Fact]
    public void Hs_Path_Still_Works_DevIdp_Style()
    {
        var cfg = JwtBase("HS256");
        var secret = Convert.ToBase64String(RandomNumberGenerator.GetBytes(48))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        cfg.JwtPublicKeyPem = secret;
        var idp = new LocalIdentityProvider(cfg);

        var token = SignHs256(secret, sub: "dev-style", roles: new[] { "operator" });
        var p = idp.PrincipalFromJwtToken(token);
        Assert.Equal("dev-style", p.SubjectId);
        Assert.True(p.Roles.Has("operator"));
    }

    [Fact]
    public async Task Hs_Token_Rejected_When_Only_Rs256_Jwks_Configured()
    {
        using var rsa = RSA.Create(2048);
        const string url = "https://jwks.test/certs-rs-only";
        var doc = BuildJwksJson(rsa, "kid-1");
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));

        var cfg = JwtBase("RS256");
        cfg.JwtJwksUrl = url;
        var idp = new LocalIdentityProvider(cfg);

        // HS256 token must not authenticate under RS256+JWKS config.
        var hsToken = SignHs256("not-the-jwks-secret-32bytes-min!!", sub: "attacker");
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + hsToken,
            },
        });
        Assert.Equal("anonymous", p.SubjectId);
        Assert.ThrowsAny<Exception>(() => idp.PrincipalFromJwtToken(hsToken));
    }

    [Fact]
    public void No_Key_Configured_Fails_Closed()
    {
        var cfg = JwtBase("HS256");
        cfg.JwtPublicKeyPem = null;
        cfg.JwtJwksUrl = null;
        var idp = new LocalIdentityProvider(cfg);
        Assert.ThrowsAny<Exception>(() => idp.PrincipalFromJwtToken(SignHs256("x")));
    }

    [Fact]
    public void ResolveKey_Empty_Url_Fails()
    {
        Assert.ThrowsAny<Exception>(() => Jwks.ResolveKey("  ", "kid"));
    }

    [Fact]
    public void Jwks_Cache_Evicts_When_Full()
    {
        // Fill cache beyond max (16) then ensure a new URL still resolves.
        using var rsa = RSA.Create(2048);
        var fetches = 0;
        Jwks.SetHttpClient(StubHttpClient(_ =>
        {
            Interlocked.Increment(ref fetches);
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(BuildJwksJson(rsa, "k"), Encoding.UTF8, "application/json"),
            };
        }));

        for (var i = 0; i < 18; i++)
        {
            var key = Jwks.ResolveKey($"https://jwks.test/evict-{i}", "k");
            Assert.NotNull(key);
        }

        Assert.True(fetches >= 18);
        // Cached hit for a recent URL should not increment further beyond one more fetch max.
        var before = fetches;
        _ = Jwks.ResolveKey("https://jwks.test/evict-17", "k");
        Assert.Equal(before, fetches);
    }

    [Fact]
    public void Jwks_Skips_Non_Rsa_Keys()
    {
        const string url = "https://jwks.test/mixed";
        using var rsa = RSA.Create(2048);
        var rsaParams = rsa.ExportParameters(false);
        var doc = JsonSerializer.Serialize(new
        {
            keys = new object[]
            {
                new Dictionary<string, string> { ["kty"] = "oct", ["kid"] = "sym", ["k"] = "AA" },
                new Dictionary<string, string>
                {
                    ["kty"] = "RSA",
                    ["kid"] = "r1",
                    ["n"] = Base64UrlEncoder.Encode(rsaParams.Modulus!),
                    ["e"] = Base64UrlEncoder.Encode(rsaParams.Exponent!),
                },
            },
        });
        Jwks.SetHttpClient(StubHttpClient(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(doc, Encoding.UTF8, "application/json"),
        }));
        var key = Jwks.ResolveKey(url, "r1");
        Assert.Equal("r1", key.KeyId);
    }

    [Fact]
    public void RsaFromJwk_RoundTrip_And_Bad_Base64()
    {
        using var rsa = RSA.Create(2048);
        var rsaParams = rsa.ExportParameters(false);
        var n = Base64UrlEncoder.Encode(rsaParams.Modulus!);
        var e = Base64UrlEncoder.Encode(rsaParams.Exponent!);
        var key = Jwks.RsaFromJwk(new Jwks.JwkKey { Kty = "RSA", N = n, E = e, Kid = "t" });
        Assert.Equal("t", key.KeyId);
        Assert.NotNull(key.Rsa);

        Assert.ThrowsAny<Exception>(() =>
            Jwks.RsaFromJwk(new Jwks.JwkKey { N = "!!!", E = e }));
        Assert.ThrowsAny<Exception>(() =>
            Jwks.RsaFromJwk(new Jwks.JwkKey { N = n, E = "!!!" }));
    }
}

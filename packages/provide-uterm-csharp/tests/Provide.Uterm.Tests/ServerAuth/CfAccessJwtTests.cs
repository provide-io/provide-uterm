//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.ServerAuth;

/// <summary>
/// CF Access JWT material sources + jwt_default_role (Go serverauth parity).
/// Never trusts Cf-Access-Authenticated-User-Email.
/// </summary>
public class CfAccessJwtTests
{
    private static (AuthConfig cfg, string secret) JwtCfg()
    {
        var cfg = new AuthConfig
        {
            Mode = "jwt",
            JwtIssuer = "provide-uterm",
            JwtAudience = "provide-uterm-server",
            JwtAlgorithms = new List<string> { "HS256" },
        };
        var secret = Convert.ToBase64String(System.Security.Cryptography.RandomNumberGenerator.GetBytes(48))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        cfg.JwtPublicKeyPem = secret;
        return (cfg, secret);
    }

    private static string MakeToken(AuthConfig cfg, string secret, string sub, string[]? roles, int ttlS = 600)
    {
        var now = DateTimeOffset.UtcNow;
        var claims = new List<Claim>
        {
            new("sub", sub),
            new(JwtRegisteredClaimNames.Iss, cfg.JwtIssuer),
            new(JwtRegisteredClaimNames.Aud, cfg.JwtAudience),
        };
        if (roles is not null)
        {
            foreach (var r in roles)
            {
                claims.Add(new Claim(cfg.JwtRolesClaim, r));
            }
        }

        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
        var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
        var token = new JwtSecurityToken(
            issuer: cfg.JwtIssuer,
            audience: cfg.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddSeconds(ttlS).UtcDateTime,
            signingCredentials: creds);
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    [Fact]
    public async Task Jwt_From_CF_Access_JWT_Assertion_Header()
    {
        var (cfg, secret) = JwtCfg();
        var token = MakeToken(cfg, secret, "cf-user@example.com", new[] { "operator" });
        var idp = new LocalIdentityProvider(cfg);

        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["CF-Access-JWT-Assertion"] = token,
            },
        });
        Assert.Equal("cf-user@example.com", p.SubjectId);
        Assert.True(p.Roles.Has("operator"));

        // Case-insensitive header name
        var p2 = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["cf-access-jwt-assertion"] = token,
            },
        });
        Assert.Equal("cf-user@example.com", p2.SubjectId);
    }

    [Fact]
    public async Task Jwt_From_CF_Authorization_Cookie()
    {
        var (cfg, secret) = JwtCfg();
        var token = MakeToken(cfg, secret, "cookie-cf-user", new[] { "admin" });
        var idp = new LocalIdentityProvider(cfg);

        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Cookies = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["CF_Authorization"] = token,
            },
        });
        Assert.Equal("cookie-cf-user", p.SubjectId);
        Assert.True(p.Roles.Has("admin"));
    }

    [Fact]
    public async Task CF_Access_Email_Header_Alone_Does_Not_Authenticate()
    {
        var (cfg, _) = JwtCfg();
        var idp = new LocalIdentityProvider(cfg);
        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Cf-Access-Authenticated-User-Email"] = "spoofed@evil.example",
            },
        });
        Assert.Equal("anonymous", p.SubjectId);
    }

    [Fact]
    public async Task Jwt_Token_Source_Precedence()
    {
        var (cfg, secret) = JwtCfg();
        var bearer = MakeToken(cfg, secret, "from-bearer", new[] { "admin" });
        var cfHdr = MakeToken(cfg, secret, "from-cf-hdr", new[] { "admin" });
        var cfCookie = MakeToken(cfg, secret, "from-cf-cookie", new[] { "admin" });
        var appCookie = MakeToken(cfg, secret, "from-app-cookie", new[] { "admin" });
        var idp = new LocalIdentityProvider(cfg);

        var p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["authorization"] = "Bearer " + bearer,
                ["CF-Access-JWT-Assertion"] = cfHdr,
            },
            Cookies = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["CF_Authorization"] = cfCookie,
                ["uterm_token"] = appCookie,
            },
        });
        Assert.Equal("from-bearer", p.SubjectId);

        p = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["CF-Access-JWT-Assertion"] = cfHdr,
            },
            Cookies = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["CF_Authorization"] = cfCookie,
                ["uterm_token"] = appCookie,
            },
        });
        Assert.Equal("from-cf-hdr", p.SubjectId);

        p = await idp.AuthenticateAsync(new AuthRequest
        {
            Cookies = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["CF_Authorization"] = cfCookie,
                ["uterm_token"] = appCookie,
            },
        });
        Assert.Equal("from-cf-cookie", p.SubjectId);
    }

    [Fact]
    public void Jwt_Default_Role_When_No_Roles_Claim()
    {
        var (cfg, secret) = JwtCfg();
        cfg.JwtDefaultRole = "operator";
        var idp = new LocalIdentityProvider(cfg);
        var token = MakeToken(cfg, secret, "user@example.com", roles: null);
        var p = idp.PrincipalFromJwtToken(token);
        Assert.Equal("user@example.com", p.SubjectId);
        Assert.True(p.Roles.Has("operator"));
        Assert.Single(p.Roles);

        // Explicit roles claim still wins
        var token2 = MakeToken(cfg, secret, "u2", new[] { "admin" });
        var p2 = idp.PrincipalFromJwtToken(token2);
        Assert.True(p2.Roles.Has("admin"));
        Assert.Single(p2.Roles);
    }

    [Fact]
    public void Jwt_Default_Role_Unknown_Falls_Back_To_Viewer()
    {
        var (cfg, _) = JwtCfg();
        cfg.JwtDefaultRole = "superuser";
        var idp = new LocalIdentityProvider(cfg);
        var roles = idp.RolesFromClaimList(Array.Empty<string>());
        Assert.True(roles.Has("viewer"));
        Assert.Single(roles);
    }

    [Fact]
    public void Cf_Access_Team_Domain_Auto_Fill()
    {
        var a = new AuthConfig
        {
            JwtIssuer = "",
            JwtJwksUrl = null,
            CfAccessTeamDomain = "myteam",
        };
        ConfigLoader.ApplyCfAccessTeamDomain(a);
        Assert.Equal("https://myteam.cloudflareaccess.com/cdn-cgi/access/certs", a.JwtJwksUrl);
        Assert.Equal("https://myteam.cloudflareaccess.com", a.JwtIssuer);

        // Explicit values win
        var a2 = new AuthConfig
        {
            JwtIssuer = "https://custom.example",
            JwtJwksUrl = "https://custom.example/jwks",
            CfAccessTeamDomain = "myteam",
        };
        ConfigLoader.ApplyCfAccessTeamDomain(a2);
        Assert.Equal("https://custom.example/jwks", a2.JwtJwksUrl);
        Assert.Equal("https://custom.example", a2.JwtIssuer);

        // Strip scheme/path
        var a3 = new AuthConfig
        {
            JwtIssuer = "",
            JwtJwksUrl = null,
            CfAccessTeamDomain = "https://other.cloudflareaccess.com/",
        };
        ConfigLoader.ApplyCfAccessTeamDomain(a3);
        Assert.Equal("https://other.cloudflareaccess.com/cdn-cgi/access/certs", a3.JwtJwksUrl);
        Assert.Equal("https://other.cloudflareaccess.com", a3.JwtIssuer);
    }
}

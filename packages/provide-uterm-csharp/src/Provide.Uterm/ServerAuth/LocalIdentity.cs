//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text.RegularExpressions;
using System.Text;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// Standard identity provider covering api_key precedence + header/jwt modes.
/// </summary>
public sealed class LocalIdentityProvider : IAuthenticator
{
    private static readonly Regex TenantPattern = new(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", RegexOptions.Compiled);
    private readonly AuthConfig _auth;
    private readonly ApiKeyStore? _apiKeys;

    public LocalIdentityProvider(AuthConfig auth, ApiKeyStore? apiKeys = null)
    {
        _auth = auth;
        _apiKeys = apiKeys;
    }

    public Task<Principal> AuthenticateAsync(AuthRequest request, CancellationToken cancellationToken = default)
    {
        _ = cancellationToken;
        var apiKeyPrincipal = PrincipalFromApiKey(request);
        if (apiKeyPrincipal is not null)
        {
            return Task.FromResult(apiKeyPrincipal);
        }

        var mode = _auth.Mode.Trim().ToLowerInvariant();
        if (mode == "header")
        {
            if (_auth.TrustedProxyIps.Count > 0 &&
                !_auth.TrustedProxyIps.Contains(request.SourceIp, StringComparer.Ordinal))
            {
                return Task.FromResult(Principal.Anonymous());
            }

            return Task.FromResult(PrincipalFromHeaderAuth(request));
        }

        if (mode is "jwt" or "dev_token")
        {
            // dev_token is mutated to jwt by SetupDevIdp; if still present, treat as jwt.
            var token = ExtractBearerToken(request);
            if (string.IsNullOrEmpty(token))
            {
                token = request.Cookie(_auth.TokenCookie);
            }

            if (string.IsNullOrEmpty(token))
            {
                return Task.FromResult(Principal.Anonymous());
            }

            try
            {
                return Task.FromResult(PrincipalFromJwtToken(token));
            }
            catch
            {
                return Task.FromResult(Principal.Anonymous());
            }
        }

        throw new InvalidOperationException($"unknown auth mode: \"{mode}\"");
    }

    public static string ExtractBearerToken(AuthRequest req)
    {
        var authorization = req.Header("authorization").Trim();
        if (authorization.Length == 0) return "";
        var parts = authorization.Split(' ', 2, StringSplitOptions.None);
        if (parts.Length != 2) return "";
        if (!parts[0].Equals("Bearer", StringComparison.OrdinalIgnoreCase)) return "";
        return parts[1].Trim();
    }

    public Principal PrincipalFromHeaderAuth(AuthRequest req)
    {
        var principal = FirstNonEmpty(
            req.Header(_auth.PrincipalHeader),
            req.Cookie(_auth.PrincipalCookie),
            "anonymous");
        var roleRaw = FirstNonEmpty(req.Header(_auth.RoleHeader), req.Cookie(_auth.RoleCookie), "");
        var rawTenant = FirstNonEmpty(req.Header(_auth.TenantHeader), req.Cookie(_auth.TenantCookie));
        var tenant = CanonicalTenantId(rawTenant);
        if (!string.IsNullOrWhiteSpace(rawTenant) && tenant is null)
        {
            return Principal.Anonymous();
        }

        return new Principal
        {
            SubjectId = principal,
            Roles = AuthRoles.FilterKnownRoles(new[] { roleRaw }),
            Scopes = new StringSet(),
            TenantId = tenant,
        };
    }

    public Principal? PrincipalFromApiKey(AuthRequest req)
    {
        if (!_auth.ApiKeysEnabled || _apiKeys is null) return null;
        var rawKey = req.Header("x-api-key").Trim();
        if (rawKey.Length == 0) return null;
        var record = _apiKeys.Validate(rawKey);
        if (record is null) return null;
        if (string.IsNullOrWhiteSpace(record.TenantId)) return null;
        var tenant = CanonicalTenantId(record.TenantId);
        if (tenant is null) return null;

        StringSet roles;
        StringSet scopes;
        if (record.Scopes.Has("admin"))
        {
            roles = StringSet.Of("admin");
            scopes = StringSet.Of("*");
        }
        else if (record.Scopes.Has("operator"))
        {
            roles = StringSet.Of("operator");
            scopes = StringSet.Of("*");
        }
        else if (record.Scopes.Has("viewer"))
        {
            roles = StringSet.Of("viewer");
            scopes = StringSet.Of("*");
        }
        else
        {
            return null;
        }

        return new Principal
        {
            SubjectId = "apikey:" + record.KeyId,
            Roles = roles,
            Scopes = scopes,
            TenantId = tenant,
            Claims = new Dictionary<string, object?> { ["key_id"] = record.KeyId, ["key_name"] = record.Name },
        };
    }

    public Principal PrincipalFromJwtToken(string token)
    {
        var handler = new JwtSecurityTokenHandler();
        var keyBytes = Encoding.UTF8.GetBytes(_auth.JwtPublicKeyPem ?? "");
        if (keyBytes.Length == 0)
        {
            throw new SecurityTokenException("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode");
        }

        var parameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = _auth.JwtIssuer,
            ValidateAudience = true,
            ValidAudience = _auth.JwtAudience,
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new SymmetricSecurityKey(keyBytes),
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromSeconds(Math.Max(0, _auth.ClockSkewSeconds)),
            ValidAlgorithms = _auth.JwtAlgorithms,
        };

        var principal = handler.ValidateToken(token, parameters, out var validatedToken);
        var subject = principal.FindFirst("sub")?.Value
                      ?? principal.FindFirst(ClaimTypes.NameIdentifier)?.Value
                      ?? "";
        subject = subject.Trim();
        if (subject.Length == 0)
        {
            throw new SecurityTokenException("sub claim is required");
        }

        var rawTenant = principal.FindFirst(_auth.JWTTenantClaim)?.Value?.Trim();
        if (rawTenant is null
            && validatedToken is JwtSecurityToken decodedJwt
            && decodedJwt.Payload.TryGetValue(_auth.JWTTenantClaim, out var fromPayload)
            && fromPayload is not null)
        {
            rawTenant = fromPayload.ToString();
        }
        var tenant = CanonicalTenantId(rawTenant);
        if (!string.IsNullOrWhiteSpace(rawTenant) && tenant is null)
        {
            throw new SecurityTokenException("invalid tenant_id claim");
        }

        var roles = new List<string>();
        foreach (var c in principal.FindAll(_auth.JwtRolesClaim).Concat(principal.FindAll(ClaimTypes.Role)))
        {
            foreach (var part in c.Value.Split(new[] { ',', ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                roles.Add(part);
            }
        }

        var scopes = new StringSet();
        foreach (var c in principal.FindAll(_auth.JwtScopesClaim))
        {
            foreach (var part in c.Value.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                scopes.Add(part);
            }
        }

        return new Principal
        {
            SubjectId = subject,
            Roles = AuthRoles.FilterKnownRoles(roles),
            Scopes = scopes,
            TenantId = tenant,
        };
    }

    private static string FirstNonEmpty(params string[] values)
    {
        foreach (var v in values)
        {
            if (!string.IsNullOrWhiteSpace(v)) return v.Trim();
        }

        return "";
    }

    private static string? CanonicalTenantId(string? tenant)
    {
        var value = tenant?.Trim();
        if (string.IsNullOrEmpty(value))
        {
            return null;
        }

        if (!TenantPattern.IsMatch(value))
        {
            return null;
        }

        return value;
    }
}

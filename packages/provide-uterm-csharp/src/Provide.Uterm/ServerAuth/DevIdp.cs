//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using Microsoft.IdentityModel.Tokens;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.ServerAuth;

/// <summary>Local-only stub IdP that mints a JWT and rewrites AuthConfig to jwt mode.</summary>
public static class DevIdp
{
    public const int DevTokenTtlS = 24 * 3600;

    public sealed class Options
    {
        public string? TokenPath { get; set; }
        public string Subject { get; set; } = "dev-user";
        public string[] Roles { get; set; } = { "admin" };
        public int TtlS { get; set; } = DevTokenTtlS;
    }

    public static string Setup(AuthConfig auth, Options? options = null)
    {
        options ??= new Options();
        var secret = Convert.ToBase64String(RandomNumberGenerator.GetBytes(48))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        auth.Mode = "jwt";
        auth.JwtPublicKeyPem = secret;
        auth.JwtAlgorithms = new List<string> { "HS256" };
        if (string.IsNullOrEmpty(auth.JwtIssuer)) auth.JwtIssuer = "provide-uterm-dev";
        if (string.IsNullOrEmpty(auth.JwtAudience)) auth.JwtAudience = "provide-uterm-server";
        if (string.IsNullOrEmpty(auth.WorkerBearerToken))
        {
            auth.WorkerBearerToken = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32))
                .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        var now = DateTimeOffset.UtcNow;
        var claims = new List<Claim>
        {
            new("sub", options.Subject),
            new(JwtRegisteredClaimNames.Iss, auth.JwtIssuer),
            new(JwtRegisteredClaimNames.Aud, auth.JwtAudience),
        };
        foreach (var role in options.Roles)
        {
            claims.Add(new Claim(auth.JwtRolesClaim, role));
        }

        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));
        var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
        var token = new JwtSecurityToken(
            issuer: auth.JwtIssuer,
            audience: auth.JwtAudience,
            claims: claims,
            notBefore: now.UtcDateTime,
            expires: now.AddSeconds(options.TtlS).UtcDateTime,
            signingCredentials: creds);
        var jwt = new JwtSecurityTokenHandler().WriteToken(token);

        var path = ResolvedTokenPath(options.TokenPath);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, jwt);
        try
        {
            // Best-effort 0600 on Unix.
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }
        }
        catch
        {
            // ignore permission errors
        }

        return jwt;
    }

    public static string ResolvedTokenPath(string? explicitPath)
    {
        if (!string.IsNullOrEmpty(explicitPath)) return explicitPath;
        var env = Environment.GetEnvironmentVariable("UTERM_DEV_TOKEN_PATH");
        if (!string.IsNullOrEmpty(env)) return env;
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".cache", "uterm", "dev_token");
    }
}

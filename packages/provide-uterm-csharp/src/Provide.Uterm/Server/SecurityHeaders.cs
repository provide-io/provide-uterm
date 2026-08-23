// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Server;

public static class SecurityHeaders
{
    private static readonly IReadOnlyDictionary<string, string> StrictDefaults = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["Content-Security-Policy"] =
            "default-src 'self'; script-src 'self' cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' cdn.jsdelivr.net fonts.googleapis.com; font-src fonts.gstatic.com; connect-src 'self' ws: wss:; img-src 'self' data:",
        ["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains",
        ["X-Frame-Options"] = "DENY",
        ["X-Content-Type-Options"] = "nosniff",
        ["Referrer-Policy"] = "strict-origin-when-cross-origin",
        ["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()",
    };

    private static readonly IReadOnlyDictionary<string, string> DevDefaults = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["X-Content-Type-Options"] = "nosniff",
    };

    /// <summary>
    /// Each key is the security config field, and each value is the emitted
    /// header, in the same order as the reference applies them.
    /// </summary>
    public static readonly IReadOnlyList<(string Field, string Header)> SecurityHeaderFields = new List<(string, string)>
    {
        ("csp", "Content-Security-Policy"),
        ("hsts", "Strict-Transport-Security"),
        ("x_frame_options", "X-Frame-Options"),
        ("x_content_type_options", "X-Content-Type-Options"),
        ("referrer_policy", "Referrer-Policy"),
        ("permissions_policy", "Permissions-Policy"),
    };

    /// <summary>
    /// Build the final header list from mode + per-header overrides.
    /// </summary>
    public static IReadOnlyList<(string Header, string Value)> ResolveSecurityHeaders(SecurityConfig cfg)
    {
        var defaults = cfg.Mode == "strict" ? StrictDefaults : DevDefaults;
        var resolved = new List<(string Header, string Value)>();
        foreach (var (field, header) in SecurityHeaderFields)
        {
            var overrideValue = ReadOverride(cfg, field);
            if (overrideValue is not null)
            {
                if (!string.IsNullOrEmpty(overrideValue))
                {
                    resolved.Add((header, overrideValue));
                }
                continue;
            }

            if (defaults.TryGetValue(header, out var fallback))
            {
                resolved.Add((header, fallback));
            }
        }

        return resolved;
    }

    private static string? ReadOverride(SecurityConfig cfg, string field) => field switch
    {
        "csp" => cfg.Csp,
        "hsts" => cfg.Hsts,
        "x_frame_options" => cfg.XFrameOptions,
        "x_content_type_options" => cfg.XContentTypeOptions,
        "referrer_policy" => cfg.ReferrerPolicy,
        "permissions_policy" => cfg.PermissionsPolicy,
        _ => null,
    };
}

public sealed class SecurityHeadersMiddleware
{
    private readonly RequestDelegate _next;
    private readonly IReadOnlyList<(string Header, string Value)> _headers;

    public SecurityHeadersMiddleware(RequestDelegate next, SecurityConfig security)
    {
        _next = next;
        _headers = SecurityHeaders.ResolveSecurityHeaders(security);
    }

    public async Task InvokeAsync(HttpContext context)
    {
        context.Response.OnStarting(() =>
        {
            foreach (var (header, value) in _headers)
            {
                context.Response.Headers[header] = value;
            }

            return Task.CompletedTask;
        });

        await _next(context).ConfigureAwait(false);
    }
}

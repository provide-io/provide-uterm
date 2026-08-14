//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using Microsoft.AspNetCore.Http;
using Provide.Telemetry;

namespace Provide.Uterm.Server;

public sealed class TelemetryMiddleware
{
    private readonly RequestDelegate _next;
    private readonly Provide.Telemetry.Logger _logger;

    public TelemetryMiddleware(RequestDelegate next)
    {
        _next = next;
        _logger = ProvideTelemetry.GetLogger("provide.uterm.server.http");
    }

    public async Task InvokeAsync(HttpContext context)
    {
        using var span = Tracing.GetTracer("provide.uterm.server").StartSpan($"{context.Request.Method} {context.Request.Path}");
        span.SetAttribute("http.method", context.Request.Method);
        span.SetAttribute("http.url", context.Request.Path.ToString());

        var sw = Stopwatch.StartNew();
        try
        {
            await _next(context);
        }
        catch (Exception ex)
        {
            span.RecordException(ex);
            throw;
        }
        finally
        {
            sw.Stop();
            span.SetAttribute("http.status_code", context.Response.StatusCode);
            _logger.Info($"{context.Request.Method} {context.Request.Path}", new Dictionary<string, object?>
            {
                ["method"] = context.Request.Method,
                ["url"] = context.Request.Path.ToString(),
                ["status"] = context.Response.StatusCode,
                ["duration_ms"] = sw.ElapsedMilliseconds,
            });
        }
    }
}

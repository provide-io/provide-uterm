//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.FileProviders;

namespace Provide.Uterm.Server;

/// <summary>
/// Static SPA/UI hosting for share/inspect/app pages (Python/Go frontend-dir parity).
/// </summary>
public sealed partial class UtermServer
{
    private void MapStaticUi(WebApplication app)
    {
        var frontendDir = _deps.FrontendDir
            ?? Environment.GetEnvironmentVariable("UTERM_FRONTEND_DIR");
        if (string.IsNullOrWhiteSpace(frontendDir) || !Directory.Exists(frontendDir))
        {
            // Minimal operator shell so inspect/share do not 404 when assets missing.
            app.MapGet("/app/{**path}", (HttpContext ctx) =>
            {
                var path = ctx.Request.RouteValues["path"]?.ToString() ?? "";
                var html =
                    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
                    "<title>provide-uterm</title></head><body>" +
                    "<div id=\"app-root\" data-uterm-shell=\"1\">provide-uterm app shell</div>" +
                    "<script id=\"app-bootstrap\" type=\"application/json\">" +
                    "{\"page_kind\":\"app\",\"path\":" + System.Text.Json.JsonSerializer.Serialize(path) + "}" +
                    "</script></body></html>";
                return Results.Content(html, "text/html; charset=utf-8");
            });
            return;
        }

        var ui = _deps.Config.Ui;
        var assets = string.IsNullOrWhiteSpace(ui.AssetsPath) ? "/ui" : ui.AssetsPath.TrimEnd('/');
        var appPath = string.IsNullOrWhiteSpace(ui.AppPath) ? "/app" : ui.AppPath.TrimEnd('/');
        var provider = new PhysicalFileProvider(Path.GetFullPath(frontendDir));
        app.UseStaticFiles(new StaticFileOptions
        {
            FileProvider = provider,
            RequestPath = assets,
        });
        app.MapGet(appPath + "/{**path}", (HttpContext ctx) =>
        {
            var index = Path.Combine(frontendDir, "index.html");
            if (File.Exists(index))
            {
                return Results.File(index, "text/html; charset=utf-8");
            }

            // Frontend dir without index — serve terminal.html if present
            var term = Path.Combine(frontendDir, "terminal.html");
            if (File.Exists(term))
            {
                return Results.File(term, "text/html; charset=utf-8");
            }

            return Results.Content(
                "<!DOCTYPE html><html><body><div id=\"app-root\">uterm</div></body></html>",
                "text/html; charset=utf-8");
        });
    }
}

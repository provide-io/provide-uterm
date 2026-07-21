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
/// App HTML routes require authentication (Go <c>authenticated</c> page handlers).
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
            app.MapGet("/app/{**path}", async (HttpContext ctx) =>
            {
                var (_, err) = await RequireAuthenticated(ctx).ConfigureAwait(false);
                if (err is not null) return err;
                var path = ctx.Request.RouteValues["path"]?.ToString() ?? "";
                return Results.Content(BuildFallbackShellHtml(path), "text/html; charset=utf-8");
            });
            return;
        }

        var ui = _deps.Config.Ui;
        var assets = string.IsNullOrWhiteSpace(ui.AssetsPath) ? "/ui" : ui.AssetsPath.TrimEnd('/');
        var appPath = string.IsNullOrWhiteSpace(ui.AppPath) ? "/app" : ui.AppPath.TrimEnd('/');
        var provider = new PhysicalFileProvider(Path.GetFullPath(frontendDir));
        // Static assets under AssetsPath remain public (same as Go static mount pattern);
        // HTML app routes require a principal.
        app.UseStaticFiles(new StaticFileOptions
        {
            FileProvider = provider,
            RequestPath = assets,
        });
        app.MapGet(appPath + "/{**path}", async (HttpContext ctx) =>
        {
            var (_, err) = await RequireAuthenticated(ctx).ConfigureAwait(false);
            if (err is not null) return err;

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

            return Results.Content(BuildFallbackShellHtml(""), "text/html; charset=utf-8");
        });
    }

    /// <summary>
    /// Minimal operator shell when no frontend dir is baked. Optional CDN xterm
    /// tags include SRI (integrity + crossorigin) when configured — parity with
    /// Go/Python UI and the CF SPA shell.
    /// </summary>
    internal string BuildFallbackShellHtml(string path)
    {
        var ui = _deps.Config.Ui;
        var xtermCss = "";
        var xtermJs = "";
        var fitJs = "";
        if (!string.IsNullOrWhiteSpace(ui.XtermCdn))
        {
            var baseUrl = ui.XtermCdn.TrimEnd('/');
            // CSS SRI only — do not reuse the CSS hash on the JS tag.
            var cssSri = string.IsNullOrWhiteSpace(ui.XtermCdnIntegrity)
                ? ""
                : $" integrity=\"{System.Net.WebUtility.HtmlEncode(ui.XtermCdnIntegrity)}\" crossorigin=\"anonymous\"";
            xtermCss = $"<link rel=\"stylesheet\" href=\"{System.Net.WebUtility.HtmlEncode(baseUrl)}/css/xterm.css\"{cssSri}>";
            xtermJs =
                $"<script src=\"{System.Net.WebUtility.HtmlEncode(baseUrl)}/lib/xterm.js\" crossorigin=\"anonymous\"></script>";
        }

        if (!string.IsNullOrWhiteSpace(ui.FitAddonCdn))
        {
            var fitBase = ui.FitAddonCdn.TrimEnd('/');
            var fitSri = string.IsNullOrWhiteSpace(ui.FitAddonCdnIntegrity)
                ? " crossorigin=\"anonymous\""
                : $" integrity=\"{System.Net.WebUtility.HtmlEncode(ui.FitAddonCdnIntegrity)}\" crossorigin=\"anonymous\"";
            fitJs = $"<script src=\"{System.Net.WebUtility.HtmlEncode(fitBase)}/lib/addon-fit.js\"{fitSri}></script>";
        }

        return
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
            "<title>provide-uterm</title>" +
            xtermCss +
            xtermJs +
            fitJs +
            "</head><body>" +
            "<div id=\"app-root\" data-uterm-shell=\"1\">provide-uterm app shell</div>" +
            "<script id=\"app-bootstrap\" type=\"application/json\">" +
            "{\"page_kind\":\"app\",\"path\":" + System.Text.Json.JsonSerializer.Serialize(path) + "}" +
            "</script></body></html>";
    }
}

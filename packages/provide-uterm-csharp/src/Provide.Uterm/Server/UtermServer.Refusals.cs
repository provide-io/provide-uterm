//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.WebUtilities;

namespace Provide.Uterm.Server;

/// <summary>
/// The refusals no handler of this server writes.
///
/// A path in no route table and a known path with an unregistered method are
/// answered by ASP.NET itself, and ASP.NET answers them with a status and
/// nothing else — no body at all. The reference (FastAPI) answers
/// <c>{"detail": "Not Found"}</c> and <c>{"detail": "Method Not Allowed"}</c>,
/// which is what a client that renders errors actually reads. Pinned by
/// conformance/live scenario 003_error_shapes; the Go port fixed the identical
/// gap in <c>server/httpjson.go routeFallback</c>.
///
/// The trap Go hit was that re-deciding 404-vs-405 by hand is a second copy of
/// the routing rules, and it drifts. Nothing here decides anything: the status
/// (and the <c>Allow</c> header a 405 carries) is whatever routing already
/// settled on, and this only supplies the body it left off — through the same
/// <c>DetailError</c> every hand-raised refusal in this server goes through, so
/// there is one error shape rather than two.
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>
    /// Give a bodiless refusal the reference's body.
    ///
    /// <see cref="StatusCodePagesExtensions.UseStatusCodePages(IApplicationBuilder, Func{StatusCodeContext, Task})"/>
    /// runs only when the response is a 4xx/5xx that has not started and
    /// carries neither content type nor content length — so every refusal this
    /// server writes itself (its unknown-session 404, its 401s) is already
    /// past it and is left exactly as it was.
    /// </summary>
    private static void UseFrameworkRefusalBodies(WebApplication app) =>
        app.UseStatusCodePages(async context =>
        {
            var status = context.HttpContext.Response.StatusCode;
            var reason = ReasonPhrases.GetReasonPhrase(status);
            if (reason.Length == 0)
            {
                // A status .NET has no phrase for is one this server never
                // raises; inventing wording for it would be a third error shape.
                return;
            }

            await DetailError(status, reason).ExecuteAsync(context.HttpContext).ConfigureAwait(false);
        });
}

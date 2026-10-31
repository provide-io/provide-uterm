//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Unicode;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Server;

/// <summary>
/// POST /api/sessions/{sessionId}/viewers/revoke (Python routes/viewers.py, Go
/// routes_viewers.go): record a share-viewer cutoff for the session, or for one
/// share subject of it when the body names one, and close every live browser
/// socket held by a share viewer it now refuses.
/// </summary>
public sealed partial class UtermServer
{
    /// <summary>
    /// Close code for a revoked share viewer: an application code (4000-4999) so
    /// a client can tell "access was taken away" from a drop or a policy refusal
    /// and stop reconnecting. 4410 echoes HTTP 410 Gone.
    /// </summary>
    internal const int ViewerRevokedCloseCode = 4410;

    /// <summary>Deliberately generic: the embedder knows why, and says so in its own words.</summary>
    internal const string ViewerRevokedReason = "viewer access revoked";

    private static readonly Provide.Telemetry.Logger ViewersLog =
        Provide.Telemetry.ProvideTelemetry.GetLogger("provide.uterm.server.viewers");

    /// <summary>
    /// Where audit records of this route go: the telemetry log, or a test's
    /// recorder.
    /// </summary>
    internal static Action<IReadOnlyDictionary<string, object?>> ViewersAudit { get; set; } =
        static fields => ViewersLog.Info("audit_event", fields);

    /// <summary>Open share-viewer event streams, ended by a revoke.</summary>
    private readonly ShareViewerStreams _viewerStreams = new();

    /// <summary>
    /// Runs just before a browser registers with the hub: a test seam for a
    /// revoke landing between the read decision and registration.
    /// </summary>
    internal static Action<string> BrowserPreRegisterHook { get; set; } = static _ => { };

    /// <summary>
    /// Runs after a share viewer's event stream registers and before its cutoff
    /// is checked: a test seam for the same race on the stream.
    /// </summary>
    internal static Action<string> SseOpenHook { get; set; } = static _ => { };

    /// <summary>
    /// How long a revoked socket gets to complete the close handshake before it
    /// is aborted, so a client that ignores the close neither stays registered
    /// nor holds a quota slot.
    /// </summary>
    internal static TimeSpan ViewerCloseGrace { get; set; } = TimeSpan.FromSeconds(2);

    /// <summary>How many browsers the hub holds for a session (tests).</summary>
    internal int HubBrowserCount(string sessionId) => _deps.Hub.Conn.BrowsersOf(sessionId).Count;

    /// <summary>The open share-viewer streams on a session (tests).</summary>
    internal int ViewerStreamCount(string sessionId) => _viewerStreams.Count(sessionId);

    private void MapViewerRoutes(WebApplication app) =>
        app.MapPost("/api/sessions/{sessionId}/viewers/revoke", (Delegate)HandleRevokeViewers);

    /// <summary>
    /// The share subject a revoke body names, or <c>null</c> to revoke every share
    /// viewer; or the refusal to answer with. No body, a blank one, <c>null</c> and
    /// <c>{}</c> all mean the whole session, as before the body existed (<c>null</c>
    /// is "no body" across the ports, as in the reference's optional bodies).
    /// Anything else must be exactly <c>{"subject": "&lt;string&gt;"}</c>: an
    /// unknown key (a misspelt <c>subject</c>) or a null subject is refused rather
    /// than read as "every viewer", which would close sockets the caller meant to
    /// leave open. Invalid UTF-8 and a byte-order mark are refused, as in the
    /// other ports.
    /// </summary>
    private static async Task<(string? Subject, IResult? Error)> RequestedSubject(HttpContext ctx, string sessionId)
    {
        var invalid = (default(string), DetailError(422, "invalid request body"));
        // Read up to one byte past the cap and no further: the largest body worth
        // reading is a 512-byte subject in its envelope.
        var buffer = new byte[AuthorizationService.ShareViewerRevokeBodyMaxBytes + 1];
        var size = 0;
        int read;
        while (size < buffer.Length
            && (read = await ctx.Request.Body.ReadAsync(buffer.AsMemory(size), ctx.RequestAborted).ConfigureAwait(false)) > 0)
        {
            size += read;
        }
        if (size > AuthorizationService.ShareViewerRevokeBodyMaxBytes) return invalid;
        // JSON's own whitespace only (RFC 8259), as every port trims.
        var raw = buffer.AsSpan(0, size).Trim(" \t\r\n"u8);
        if (raw.IsEmpty) return (null, null);
        if (!Utf8.IsValid(raw) || raw.StartsWith(Encoding.UTF8.Preamble)) return invalid;
        JsonElement? named = null;
        try
        {
            using var doc = JsonDocument.Parse(raw.ToArray());
            if (doc.RootElement.ValueKind == JsonValueKind.Null) return (null, null);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return invalid;
            foreach (var field in doc.RootElement.EnumerateObject())
            {
                if (field.Name != "subject") return invalid;
                // The last of a repeated key wins, as in the other ports' parsers.
                named = field.Value.Clone();
            }
        }
        catch (JsonException)
        {
            return invalid;
        }
        if (named is not { } value) return (null, null);
        if (value.ValueKind != JsonValueKind.String) return invalid;
        string subject;
        try
        {
            subject = value.GetString()!;
        }
        catch (InvalidOperationException)
        {
            // JSON can spell a lone surrogate, which is not text and which
            // System.Text.Json will not decode: malformed, as in the other ports.
            return invalid;
        }
        return AuthorizationService.IsShareSubjectOf(subject, sessionId)
            ? (subject, null)
            : (null, DetailError(422, "subject must be a share subject of this session"));
    }

    private async Task<IResult> HandleRevokeViewers(HttpContext ctx, string sessionId)
    {
        var (p, _, err) = await TryGatedSession(ctx, sessionId, "session.control.update").ConfigureAwait(false);
        if (err is not null) return err;
        var (subject, refused) = await RequestedSubject(ctx, sessionId).ConfigureAwait(false);
        if (refused is not null) return refused;
        double revokedAt;
        if (subject is not null)
        {
            try
            {
                revokedAt = _deps.Authz.RevokeShareViewerSubject(sessionId, subject, _clock.Wall());
            }
            catch (ShareViewerSubjectRevocationsFullException)
            {
                return DetailError(429, "too many viewers of this session revoked one by one; revoke them all instead");
            }
            catch (ShareViewerRevocationsFullException)
            {
                return DetailError(429, "too many viewers revoked one by one; try again later");
            }
        }
        else if (RevokeSession(p!, sessionId) is { } cutoff)
        {
            revokedAt = cutoff;
        }
        else
        {
            return DetailError(429, "too many sessions with revoked viewers; try again later");
        }

        // By principal alone: a provider can resolve a share viewer to a role
        // other than viewer, and what is revoked is the token, not the role. The
        // predicate is the same either way: a subject cutoff refuses that
        // subject's sockets and nobody else's.
        var targets = _deps.Hub.Conn.BrowsersOf(sessionId)
            .Where(b => b.Ws is BrowserWsConn conn && _deps.Authz.ShareViewerRevoked(conn.Principal, sessionId))
            .Select(b => (BrowserWsConn)b.Ws)
            .ToList();
        await Task.WhenAll(targets.Select(conn =>
            conn.CloseOutputAsync((WebSocketCloseStatus)ViewerRevokedCloseCode, ViewerRevokedReason))).ConfigureAwait(false);
        // Live event streams opened with a revoked token end too, and count.
        var streams = _viewerStreams.Revoke(_deps.Authz, sessionId);
        var answer = new Dictionary<string, object?>
        {
            ["session_id"] = sessionId,
            ["revoked_at"] = revokedAt,
            ["closed"] = targets.Count + streams,
            ["close_code"] = ViewerRevokedCloseCode,
            ["close_reason"] = ViewerRevokedReason,
        };
        // The subject that was applied, so a caller can see its body was honoured.
        // Absent for a whole-session revoke, whose answer keeps its original shape.
        if (subject is not null) answer["subject"] = subject;
        return Results.Json(answer, JsonOpts);
    }

    /// <summary>
    /// Record the cutoff for every share viewer of the session and return it, or
    /// <c>null</c> when the cutoff table is full.
    /// </summary>
    private double? RevokeSession(Principal p, string sessionId)
    {
        // The embedder's service token is a global admin and revokes for
        // everyone, so only other callers are held to a per-principal share.
        var revoker = _deps.Authz.IsAdmin(p) ? null : p.SubjectId;
        try
        {
            // The cap made room by dropping this principal's oldest cutoff, so
            // that session's earlier share tokens are admitted again: say so.
            return _deps.Authz.RevokeShareViewers(sessionId, _clock.Wall(), revoker, evicted =>
                ViewersAudit(new Dictionary<string, object?>
                {
                    ["action"] = "session.viewers.cutoff_evicted",
                    ["principal"] = p.SubjectId,
                    ["session_id"] = evicted,
                    ["revoked_session_id"] = sessionId,
                }));
        }
        catch (ShareViewerRevocationsFullException)
        {
            return null;
        }
    }
}

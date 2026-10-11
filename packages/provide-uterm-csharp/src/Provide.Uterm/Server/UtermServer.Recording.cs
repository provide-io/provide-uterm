//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.AspNetCore.Http;
using Provide.Uterm.Recording;

namespace Provide.Uterm.Server;

/// <summary>
/// Thin session recording HTTP surface — parity with Python routes/sessions.py
/// and Go server/server_recording.go: annotate + meta / entries / download.
/// </summary>
public sealed partial class UtermServer
{
    private static readonly HashSet<string> ValidSeverities = new(StringComparer.Ordinal)
    {
        "info", "warning", "high", "critical",
    };

    private async Task<IResult> HandleAnnotateSession(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId)) return DetailError(422, "invalid session_id");
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        if (!_deps.Authz.CanMutateSession(p, def, "session.control.update"))
        {
            return DetailError(403, "insufficient privileges");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var label = Str(body, "label").Trim();
        if (string.IsNullOrEmpty(label))
        {
            return DetailError(400, "label is required");
        }

        var severity = Str(body, "severity", "info");
        if (string.IsNullOrEmpty(severity)) severity = "info";
        if (!ValidSeverities.Contains(severity))
        {
            return DetailError(400, "invalid severity: " + severity);
        }

        var description = Str(body, "description");
        var annotationData = new Dictionary<string, object?>
        {
            ["label"] = label,
            ["description"] = description,
            ["severity"] = severity,
            ["source"] = "agent",
            ["principal"] = p.SubjectId,
        };

        var (ts, seq) = await RecordAnnotationAsync(sessionId, annotationData).ConfigureAwait(false);
        return Results.Json(new { ts, seq }, JsonOpts);
    }

    /// <summary>
    /// Write one operator annotation to a session: the hub's event ring (what a
    /// watcher sees live) and the recording store. Only the annotate route comes
    /// through here; the detector's annotations take
    /// <see cref="RecordDetectedAnnotationAsync"/>.
    /// </summary>
    private async Task<(double Ts, int Seq)> RecordAnnotationAsync(
        string sessionId, Dictionary<string, object?> annotationData)
    {
        var ts = _clock.Wall();
        var evt = _deps.Hub.AppendEventData(sessionId, "annotation", annotationData);
        var seq = 0;
        if (evt.TryGetValue("seq", out var seqObj) && seqObj is int si)
        {
            seq = si;
        }
        else if (seqObj is long sl)
        {
            seq = (int)sl;
        }

        if (evt.TryGetValue("ts", out var tsObj) && tsObj is double td)
        {
            ts = td;
        }

        await _recording.AppendEventsAsync(sessionId, new[]
        {
            new Event
            {
                ["ts"] = ts,
                ["event"] = "annotation",
                ["data"] = annotationData,
                ["session_id"] = sessionId,
            },
        }).ConfigureAwait(false);
        return (ts, seq);
    }

    /// <summary>
    /// The redactor every automatic annotation passes before it is recorded:
    /// the reference's <c>_build_recording_redactor(recording.redact_sensitive)</c>
    /// (<c>server/runtime_helpers.py</c>) — the default rule set when
    /// <c>redact_sensitive</c> is on (the default), none when it is off.
    /// </summary>
    private string RedactForRecording(string text) =>
        _deps.Config.Recording.RedactSensitive
            ? (_recordingRedactor ??= new Redaction.StreamRedactor(Redaction.RedactionDefaults.DefaultRules()))
                .Redact(text)
            : text;

    /// <summary>Built on first use; it holds no state, so a racing second build is harmless.</summary>
    private Redaction.StreamRedactor? _recordingRedactor;

    /// <summary>
    /// Write one detector annotation (<see cref="SessionAnnotator"/>) to the
    /// session's recording, and nowhere else.
    ///
    /// <para>The reference's runtime logs its automatic annotations only to the
    /// recording logger (<c>_log_snapshot</c>, <c>_scan_output</c> and
    /// <c>_log_send</c> each call <c>logger.log_event("annotation", ...)</c>):
    /// they never enter the hub's live event ring. They used to come through
    /// <see cref="RecordAnnotationAsync"/>, which put each one in the ring — where
    /// every watcher reads it — and wrote it to the store unredacted. A rule
    /// whose description embeds the match (up to 80 characters of it) could
    /// carry a credential both places.</para>
    ///
    /// <para>Every string field passes the recording redactor first. That is
    /// stricter than the reference as written: its <c>SessionLogger</c> redacts
    /// keys, screens and wire text, but <c>log_event</c> writes its payload as
    /// given.</para>
    /// </summary>
    private Task RecordDetectedAnnotationAsync(string sessionId, Dictionary<string, object?> annotationData)
    {
        var data = new Dictionary<string, object?>(annotationData.Count);
        foreach (var (key, value) in annotationData)
        {
            data[key] = value is string text ? RedactForRecording(text) : value;
        }

        return _recording.AppendEventsAsync(sessionId, new[]
        {
            new Event
            {
                ["ts"] = _clock.Wall(),
                ["event"] = "annotation",
                ["data"] = data,
                ["session_id"] = sessionId,
            },
        });
    }

    private async Task<IResult> HandleRecordingMeta(HttpContext ctx, string sessionId)
    {
        var gate = await RecordingGate(ctx, sessionId).ConfigureAwait(false);
        if (gate is not null) return gate;

        var meta = await _recording.RecordingMetaAsync(sessionId).ConfigureAwait(false);
        return Results.Json(new
        {
            session_id = meta.SessionId,
            exists = meta.Exists,
            size_bytes = meta.SizeBytes,
            path = string.IsNullOrEmpty(meta.Path) ? null : meta.Path,
        }, JsonOpts);
    }

    private async Task<IResult> HandleRecordingEntries(HttpContext ctx, string sessionId)
    {
        var gate = await RecordingGate(ctx, sessionId).ConfigureAwait(false);
        if (gate is not null) return gate;

        var limit = 200;
        if (int.TryParse(ctx.Request.Query["limit"], out var lim))
        {
            // Match Go queryInt clamp 1..500 (default 200); Python FastAPI rejects 0.
            if (lim < 1 || lim > 500)
            {
                return DetailError(422, "limit must be between 1 and 500");
            }

            limit = lim;
        }

        int? offset = null;
        if (ctx.Request.Query.ContainsKey("offset"))
        {
            if (!int.TryParse(ctx.Request.Query["offset"], out var off) || off < 0)
            {
                return DetailError(422, "offset must be a non-negative integer");
            }

            offset = off;
        }

        var eventFilter = ctx.Request.Query["event"].ToString();
        var entries = await _recording.GetEntriesAsync(sessionId, new Query
        {
            Limit = limit,
            Offset = offset,
            Event = eventFilter ?? "",
        }).ConfigureAwait(false);

        return Results.Json(entries, JsonOpts);
    }

    private async Task<IResult> HandleRecordingDownload(HttpContext ctx, string sessionId)
    {
        var gate = await RecordingGate(ctx, sessionId).ConfigureAwait(false);
        if (gate is not null) return gate;

        var path = await _recording.GetPathAsync(sessionId).ConfigureAwait(false);
        if (string.IsNullOrEmpty(path) || !File.Exists(path))
        {
            return DetailError(404, "recording not available");
        }

        if (!RecordingPathAllowed(path, _deps.Config.Recording.Directory))
        {
            return DetailError(404, "recording not available");
        }

        var name = Path.GetFileName(path);
        return Results.File(path, contentType: "application/json", fileDownloadName: name);
    }

    /// <summary>Returns null when authorized; otherwise an error <see cref="IResult"/>.</summary>
    private async Task<IResult?> RecordingGate(HttpContext ctx, string sessionId)
    {
        if (!SafeId.IsMatch(sessionId))
        {
            return DetailError(422, "invalid session_id");
        }

        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (!_deps.Registry.TryGetDefinition(sessionId, out var def))
        {
            return DetailError(404, "unknown session: " + sessionId);
        }

        if (!_deps.Authz.CanReadRecording(p, def))
        {
            return DetailError(403, "insufficient privileges");
        }

        return null;
    }

    /// <summary>
    /// Path confinement: resolved path must live under the configured recording directory
    /// (Python path.resolve().is_relative_to / Go recordingPathAllowed).
    /// </summary>
    internal static bool RecordingPathAllowed(string path, string directory)
    {
        if (string.IsNullOrWhiteSpace(directory)) return false;
        try
        {
            var absPath = Path.GetFullPath(path);
            var absDir = Path.GetFullPath(directory);
            if (!absDir.EndsWith(Path.DirectorySeparatorChar) && !absDir.EndsWith(Path.AltDirectorySeparatorChar))
            {
                absDir += Path.DirectorySeparatorChar;
            }

            return absPath.StartsWith(absDir, StringComparison.OrdinalIgnoreCase)
                   || string.Equals(
                       Path.GetFullPath(path),
                       Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                       StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }
}

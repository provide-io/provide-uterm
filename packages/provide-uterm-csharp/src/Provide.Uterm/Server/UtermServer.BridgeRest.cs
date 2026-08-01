//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.DeckMux;
using Provide.Uterm.Hub;
using Provide.Uterm.Recording;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Server;

// UtermServer: hijack REST handlers plus auth/validation and JSON body helpers.
public sealed partial class UtermServer
{
    private IResult HandleHealth()
    {
        if (!_ready)
        {
            return Results.Json(new
            {
                status = "starting",
                ok = false,
                ready = false,
                service = "uterm-server",
            }, statusCode: 503);
        }

        var backend = string.IsNullOrEmpty(_deps.Config.ControlPlane.Backend)
            ? "memory"
            : _deps.Config.ControlPlane.Backend;
        var uptime = Math.Round((_clock.Wall() - _startTime) * 100) / 100;
        var active = _deps.Registry.ListWithDefinitions().Count;
        return Results.Json(new
        {
            status = "ok",
            ok = true,
            ready = true,
            service = "uterm-server",
            version = _deps.Version,
            uptime_s = uptime,
            active_sessions = active,
            control_plane_backend = backend,
        });
    }

    private async Task<IResult> HandleHijackAcquire(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var clientId = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        if (!_deps.Hub.AllowRestAcquireFor(clientId))
        {
            _deps.Hub.Metric("rest_acquire_rate_limited_total", 1);
            return BridgeError(429, "rate_limited");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var owner = Str(body, "owner", "operator");
        if (string.IsNullOrWhiteSpace(owner)) owner = "operator";
        var leaseS = StateStore.ClampLease(Int(body, "lease_s", 90));
        await _deps.Hub.CleanupExpiredHijackAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);

        var hijackId = NewHijackId();
        var wallNow = _clock.Wall();
        var monoNow = _clock.Monotonic();
        var (ok, reason) = await _deps.Hub.TryAcquireRestHijackAsync(workerId, owner, leaseS, hijackId, monoNow, ctx.RequestAborted)
            .ConfigureAwait(false);
        if (!ok)
        {
            return BridgeError(409, AcquireErrorMessage(reason));
        }

        _deps.Hub.Metric("hijack_acquires_total", 1);
        _deps.Hub.AppendEventData(workerId, "hijack_acquired", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["owner"] = owner,
            ["lease_s"] = leaseS,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);

        var session = _deps.Hub.GetRestSession(workerId, hijackId);
        if (session is not null)
        {
            session.AcquiredBy = p.SubjectId;
        }

        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            lease_expires_at = wallNow + leaseS,
            owner,
        }, JsonOpts);
    }

    private async Task<IResult> HandleHijackHeartbeat(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var hs = _deps.Hub.GetRestSession(workerId, hijackId);
        if (hs is null) return BridgeError(404, "Invalid or expired hijack session.");

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var leaseS = StateStore.ClampLease(Int(body, "lease_s", 90));
        var newExpires = _deps.Hub.ExtendHijackLease(workerId, hijackId, hs.Owner, leaseS, _clock.Monotonic());
        if (newExpires is null) return BridgeError(404, "Invalid or expired hijack session.");

        _deps.Hub.AppendEventData(workerId, "hijack_heartbeat", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["lease_s"] = leaseS,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            lease_expires_at = _clock.Wall() + leaseS,
        }, JsonOpts);
    }

    private async Task<IResult> HandleHijackSend(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        if (!AllowRestWrite(ctx, "rest_send_rate_limited_total", out var limited))
        {
            return limited!;
        }

        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        var body = await ReadJson(ctx).ConfigureAwait(false);
        var keys = Str(body, "keys");
        var (ok, reason) = await _deps.Hub.Conn.SendRestInputAsync(workerId, hijackId, keys, ctx.RequestAborted)
            .ConfigureAwait(false);
        if (!ok) return BridgeError(409, reason);
        _deps.Hub.AppendEventData(workerId, "hijack_send", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
            ["n"] = keys.Length,
        });
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId }, JsonOpts);
    }

    private async Task<IResult> HandleHijackStep(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        // Step is a write into a hijacked worker, so it is metered like one, and
        // it spends the *send* budget rather than a budget of its own — the
        // reference charges `allow_rest_send_for` from the step route
        // (bridge/routes/rest.py:429), as does Go (server/bridge_rest2.go:97).
        // The refusal is still counted under step's own name.
        if (!AllowRestWrite(ctx, "rest_step_rate_limited_total", out var limited))
        {
            return limited!;
        }

        var (sent, reason, freshExpires) = await _deps.Hub.Conn.SendRestControlAsync(
            workerId, hijackId, new Dictionary<string, object?>
        {
            ["type"] = "control",
            ["action"] = "step",
            ["hijack_id"] = hijackId,
            ["ts"] = _clock.Wall(),
        }, ctx.RequestAborted).ConfigureAwait(false);
        if (!sent)
        {
            if (reason == "invalid_hijack")
            {
                return BridgeError(404, "Invalid or expired hijack session.");
            }
            return BridgeError(409, "No worker connected for this session.");
        }

        _deps.Hub.AppendEventData(workerId, "hijack_step", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
        });
        _deps.Hub.Metric("hijack_steps_total", 1);
        var leaseExpiresAt = _clock.Wall() + (freshExpires!.Value - _clock.Monotonic());
        return Results.Json(new
        {
            ok = true,
            worker_id = workerId,
            hijack_id = hijackId,
            lease_expires_at = leaseExpiresAt,
        }, JsonOpts);
    }

    private async Task<IResult> HandleHijackRelease(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.hijack").ConfigureAwait(false);
        if (authError is not null) return authError;

        var (released, _) = await _deps.Hub.ReleaseRestHijackAsync(
            workerId, hijackId, ctx.RequestAborted).ConfigureAwait(false);
        if (!released)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        _deps.Hub.AppendEventData(workerId, "hijack_released", new Dictionary<string, object?>
        {
            ["hijack_id"] = hijackId,
        });
        await _deps.Hub.BroadcastHijackStateAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId }, JsonOpts);
    }

    private async Task<IResult> HandleHijackSnapshot(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.read").ConfigureAwait(false);
        if (authError is not null) return authError;
        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        // Ask the worker for the screen as it is now, the way the reference does
        // (bridge/routes/rest.py: hijack_snapshot → hub.wait_for_snapshot, whose
        // first act is request_snapshot). A worker that answers in process — the
        // session's own connector, bridged by LocalWorkerLink — has stored its
        // answer by the time this returns, so what the lease reads back includes
        // the keys it just sent. A worker across a socket answers when it
        // answers; this port reads the last snapshot it filed rather than
        // holding the request open for one, which is the reference's poll loop
        // and not yet ported.
        await _deps.Hub.Presence.RequestSnapshotAsync(workerId, ctx.RequestAborted).ConfigureAwait(false);
        var snap = _deps.Hub.Router.GetLastSnapshot(workerId) ?? new Dictionary<string, object?>
        {
            ["text"] = "",
            ["cols"] = 80,
            ["rows"] = 25,
        };
        return Results.Json(new { ok = true, worker_id = workerId, hijack_id = hijackId, snapshot = snap }, JsonOpts);
    }

    private async Task<IResult> HandleHijackEvents(HttpContext ctx, string workerId, string hijackId)
    {
        if (!ValidateIds(workerId, hijackId, out var err)) return err!;
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.read").ConfigureAwait(false);
        if (authError is not null) return authError;
        if (_deps.Hub.GetRestSession(workerId, hijackId) is null)
        {
            return BridgeError(404, "Invalid or expired hijack session.");
        }

        var after = int.TryParse(ctx.Request.Query["after_seq"], out var a) ? a : 0;
        var limit = int.TryParse(ctx.Request.Query["limit"], out var l) ? l : 200;
        var events = _deps.Hub.Router.GetRecentEvents(workerId, limit, after);
        return Results.Json(new { ok = true, events }, JsonOpts);
    }

    private async Task<IResult> HandleInputMode(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        var (p, authError) = await RequireHubAuthz(ctx, workerId, "session.control.mode").ConfigureAwait(false);
        if (authError is not null) return authError;
        var body = await ReadJson(ctx).ConfigureAwait(false);
        var mode = Str(body, "input_mode", InputModes.Hijack);
        var (ok, reason) = _deps.Hub.Router.SetInputMode(workerId, mode);
        if (!ok) return InputModeError(reason);
        return Results.Json(new { ok = true, worker_id = workerId, input_mode = mode }, JsonOpts);
    }

    /// <summary>
    /// The reference's words for a refused mode change
    /// (<c>bridge/routes/rest_workerctl.py:62-69</c>): 404 when no worker is
    /// registered, 409 for the one refusal the hub makes on its own — a switch
    /// to <c>open</c> under a live lease — and both in the hub routes'
    /// <c>error</c> envelope rather than the <c>detail</c> one.
    ///
    /// A mode that is neither <c>open</c> nor <c>hijack</c> never reaches the
    /// hub there: the request body is a model whose <c>input_mode</c> is
    /// validated on the way in (<c>bridge/models.py InputModeRequest</c>), so
    /// it is answered as a malformed request, in the validation envelope, with
    /// the same wording the session route uses for the same mistake.
    /// </summary>
    private static IResult InputModeError(string reason) => reason switch
    {
        "invalid_mode" => DetailError(422, "input_mode must be 'open' or 'hijack'"),
        "no_worker" => BridgeError(404, "No worker registered."),
        _ => BridgeError(409, "Cannot switch to open while hijack is active."),
    };

    private async Task<IResult> HandleDisconnectWorker(HttpContext ctx, string workerId)
    {
        if (!SafeId.IsMatch(workerId)) return DetailError(422, "invalid worker_id");
        // Authentication before the role check, as on every other hub route:
        // the reference's admin arm (app/hub_authz.py:97-100) runs inside a
        // router already mounted behind _require_authenticated.
        var (p, authError) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (authError is not null) return authError;
        if (!_deps.Authz.IsAdmin(p))
        {
            return DetailError(403, "admin role required");
        }

        var ok = _deps.Hub.Conn.DisconnectWorker(workerId);
        return Results.Json(new { ok, worker_id = workerId }, JsonOpts);
    }

    private async Task<Principal> Authenticate(HttpContext ctx)
    {
        var req = new AuthRequest
        {
            SourceIp = ctx.Connection.RemoteIpAddress?.ToString() ?? "",
        };
        foreach (var h in ctx.Request.Headers)
        {
            req.Headers[h.Key] = h.Value.ToString();
        }

        foreach (var c in ctx.Request.Cookies)
        {
            req.Cookies[c.Key] = c.Value;
        }

        return await _deps.Auth.AuthenticateAsync(req, ctx.RequestAborted).ConfigureAwait(false);
    }

    /// <summary>
    /// Go/Python <c>require_authenticated</c> parity: anonymous principal → 401.
    /// </summary>
    private async Task<(Principal Principal, IResult? Error)> RequireAuthenticated(HttpContext ctx)
    {
        var p = await Authenticate(ctx).ConfigureAwait(false);
        if (string.Equals(p.SubjectId, "anonymous", StringComparison.Ordinal))
        {
            return (p, DetailError(401, "authentication required"));
        }

        return (p, null);
    }

    /// <summary>
    /// The two gates every <c>/worker/{id}/...</c> route passes, in the order
    /// the reference mounts them: <c>_require_authenticated</c> first, then
    /// <c>_require_hub_route_authz</c>
    /// (<c>app/routes_wiring.py:47-50</c>). The order is the point. A caller who
    /// presented no credential is nobody and is told so (401) before any session
    /// is looked up; only a caller the server did authenticate can be told they
    /// hold the wrong role (403). Deciding both at once would let an
    /// unauthenticated caller read session state out of which refusal they got.
    /// </summary>
    private async Task<(Principal Principal, IResult? Error)> RequireHubAuthz(
        HttpContext ctx, string workerId, string capability)
    {
        var (p, authError) = await RequireAuthenticated(ctx).ConfigureAwait(false);
        if (authError is not null)
        {
            return (p, authError);
        }

        return AuthorizeHub(p, workerId, capability, out var error) ? (p, null) : (p, error);
    }

    private bool AuthorizeHub(Principal p, string workerId, string capability, out IResult? error)
    {
        if (!_deps.Registry.TryGetDefinition(workerId, out var def))
        {
            // A worker nobody registered is absent, and absent is what the
            // caller is told — in the session routes' `detail` envelope, and
            // calling it a session even here, because that is what the
            // reference's hub authz says (app/hub_authz.py:108-110). It has no
            // ad-hoc arm: a worker with no SessionDefinition has no visibility
            // policy to consult, so there is nothing to authorize against. The
            // arm this port used to have auto-registered the worker for any
            // admin, which turned "does not exist" into "exists, nobody home"
            // (409) and let a typo'd worker id mint a session.
            error = DetailError(404, "unknown session: " + workerId);
            return false;
        }

        if (capability == "session.read")
        {
            if (!_deps.Authz.CanReadSession(p, def))
            {
                error = DetailError(403, "insufficient privileges");
                return false;
            }
        }
        else if (!_deps.Authz.CanMutateSession(p, def, capability))
        {
            // For hijack on public sessions owned by admin, also allow operators that own the session.
            error = DetailError(403, "insufficient privileges");
            return false;
        }

        error = null;
        return true;
    }

    private static bool ValidateIds(string workerId, string hijackId, out IResult? error)
    {
        if (!SafeId.IsMatch(workerId))
        {
            error = DetailError(422, "invalid worker_id");
            return false;
        }

        if (!HijackIdPattern.IsMatch(hijackId))
        {
            error = DetailError(422, "invalid hijack_id");
            return false;
        }

        error = null;
        return true;
    }

    /// <summary>
    /// Apply a worker's <c>worker_hello</c> frame: its announced input mode and
    /// negotiated protocol version.
    /// </summary>
    /// <remarks>
    /// An unrecognised <c>input_mode</c> is counted and ignored rather than
    /// refused, as the reference does — a worker that announces nonsense is not
    /// a reason to drop a working session. A refused mode change is counted too,
    /// because it means an operator's decision held against a reconnect and
    /// somebody watching needs to see that rather than infer it.
    ///
    /// Both are logged as well as counted, through <see cref="TermHubConfig.OnLog"/>
    /// — see there for why a counter alone was not enough to debug a stuck session.
    /// </remarks>
    private async Task<bool> ApplyWorkerHelloAsync(
        string workerId,
        IWorkerWs worker,
        Dictionary<string, object?> hello,
        CancellationToken cancellationToken)
    {
        var mode = hello.TryGetValue("input_mode", out var raw) ? raw?.ToString() : null;
        if (mode is not (InputModes.Hijack or InputModes.Open))
        {
            if (!_deps.Hub.Conn.TryAcceptWorkerFrame(workerId, worker)) return false;
            if (mode is not null)
            {
                _deps.Hub.Metric("worker_hello_invalid_mode_total", 1);
                _deps.Hub.Log("warning", $"worker_hello_invalid_mode worker_id={workerId} "
                    + $"input_mode='{mode}' — expected 'hijack' or 'open', ignoring");
            }

            return true;
        }

        int? protocolVersion = null;
        if (hello.TryGetValue("protocol_version", out var version) && version is not null
            && int.TryParse(version.ToString(), out var parsed))
        {
            protocolVersion = parsed;
        }

        var result = _deps.Hub.Conn.SetWorkerHelloFrame(workerId, worker, mode, protocolVersion);
        if (result.Applied)
        {
            await _deps.Hub.Conn.BroadcastHijackStateAsync(workerId, cancellationToken).ConfigureAwait(false);
        }

        // No `else` counting the refusal: `SetWorkerHello` counts its own
        // decision, and counting it here as well would double every refusal
        // that arrives over this route.
        return result.Current;
    }

    private static IResult DetailError(int status, string detail) =>
        Results.Json(new { detail }, statusCode: status);

    /// <summary>
    /// The REST send budget, charged for every route that writes to a hijacked
    /// worker (<c>send</c> and <c>step</c>), with a per-route counter name.
    ///
    /// Called after authn/authz and before the lease lookup, which is the order
    /// the reference mounts it (bridge/routes/rest.py:345, :429 — both precede
    /// <c>get_rest_session</c>). The position is observable: an over-budget
    /// request for a lease nobody holds answers 429, not 404, so a caller
    /// cannot enumerate lease ids on somebody else's budget.
    /// </summary>
    private bool AllowRestWrite(HttpContext ctx, string metric, out IResult? error)
    {
        var clientId = ctx.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        if (_deps.Hub.AllowRestSendFor(clientId))
        {
            error = null;
            return true;
        }

        _deps.Hub.Metric(metric, 1);
        error = BridgeError(429, "rate_limited");
        return false;
    }

    /// <summary>
    /// The lease routes' refusal envelope: the <c>error</c> key and nothing
    /// else, exactly as the reference writes it
    /// (<c>bridge/routes/rest.py</c>, every <c>JSONResponse({"error": ...})</c>).
    /// The success bodies of those same routes do carry <c>ok: true</c> — that
    /// is the flag a client branches on before it types into somebody's
    /// terminal. Repeating it as <c>ok: false</c> on a refusal invents a second
    /// envelope that no reference client reads and that conformance/live
    /// scenarios 006/007 pin against.
    /// </summary>
    private static IResult BridgeError(int status, string error) =>
        Results.Json(new { error }, statusCode: status);

    // The reference's wording for each acquire refusal, verbatim
    // (bridge/routes/rest.py:213-217 error_msgs).
    private static string AcquireErrorMessage(string reason) => reason switch
    {
        "no_worker" => "No worker connected for this session.",
        "open_mode" => "Hijack not available in open input mode.",
        "already_hijacked" => "Worker is already hijacked.",
        _ => reason,
    };

    private static string NewHijackId() =>
        Convert.ToHexString(RandomNumberGenerator.GetBytes(8)).ToLowerInvariant();

    private static async Task<Dictionary<string, JsonElement>> ReadJson(HttpContext ctx)
    {
        if (ctx.Request.ContentLength is null or 0)
        {
            return new Dictionary<string, JsonElement>();
        }

        try
        {
            var doc = await JsonSerializer.DeserializeAsync<Dictionary<string, JsonElement>>(ctx.Request.Body)
                .ConfigureAwait(false);
            return doc ?? new Dictionary<string, JsonElement>();
        }
        catch
        {
            return new Dictionary<string, JsonElement>();
        }
    }

    private static string Str(Dictionary<string, JsonElement> body, string key, string dflt = "")
    {
        if (!body.TryGetValue(key, out var el)) return dflt;
        return el.ValueKind == JsonValueKind.String ? el.GetString() ?? dflt : el.ToString();
    }

    private static int Int(Dictionary<string, JsonElement> body, string key, int dflt)
    {
        if (!body.TryGetValue(key, out var el)) return dflt;
        return el.ValueKind switch
        {
            JsonValueKind.Number when el.TryGetInt32(out var i) => i,
            JsonValueKind.String when int.TryParse(el.GetString(), out var s) => s,
            _ => dflt,
        };
    }
}

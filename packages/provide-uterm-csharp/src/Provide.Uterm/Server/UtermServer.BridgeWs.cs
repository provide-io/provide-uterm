//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Server;

// UtermServer: browser/worker WebSocket endpoints and the browser WS connection adapter.
public sealed partial class UtermServer
{
    private async Task HandleBrowserWs(HttpContext ctx, string workerId)
    {
        if (!ctx.WebSockets.IsWebSocketRequest)
        {
            ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }

        if (!SafeId.IsMatch(workerId))
        {
            ctx.Response.StatusCode = StatusCodes.Status422UnprocessableEntity;
            return;
        }

        // UTERM_TEST_MODE=1: multi-backend Playwright e2e — admin for any worker_id.
        var testMode = string.Equals(
            Environment.GetEnvironmentVariable("UTERM_TEST_MODE"), "1", StringComparison.Ordinal);
        Principal p;
        string role;
        if (testMode)
        {
            p = new Principal { SubjectId = "test-admin", Roles = StringSet.Of("admin") };
            role = "admin";
        }
        else
        {
            p = await Authenticate(ctx).ConfigureAwait(false);
            role = "viewer";
            if (!_deps.Registry.TryGetDefinition(workerId, out var def))
            {
                ctx.Response.StatusCode = StatusCodes.Status404NotFound;
                return;
            }
            if (!_deps.Authz.CanReadSession(p, def))
            {
                ctx.Response.StatusCode = StatusCodes.Status403Forbidden;
                return;
            }

            role = _deps.Authz.ResolveBrowserRole(p, def);
        }

        using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var conn = new BrowserWsConn(ws);
        // Match Python/Go: register, then hello from registry state + immediate hijack_state.
        Dictionary<string, object?> state;
        try
        {
            state = _deps.Hub.Conn.RegisterBrowser(
                workerId, conn, role, deferBroadcast: true, principalSubjectId: p.SubjectId);
        }
        catch (BrowserRegistrationException ex)
        {
            await WebSocketCloseHandler.CloseAndTerminateAsync(
                    ws, (WebSocketCloseStatus)ex.CloseCode, ex.Message)
                .ConfigureAwait(false);
            return;
        }

        try
        {
            if (_deps.BrowserSetupHook is not null)
            {
                await _deps.BrowserSetupHook().ConfigureAwait(false);
            }
            var canHijack = role is "admin";
            static bool StateBool(IReadOnlyDictionary<string, object?> d, string key) =>
                d.TryGetValue(key, out var v) && v is true;
            static string StateStr(IReadOnlyDictionary<string, object?> d, string key, string fallback) =>
                d.TryGetValue(key, out var v) && v is string s && !string.IsNullOrEmpty(s) ? s : fallback;

            var resumeToken = MintResumeToken(workerId, role, conn);
            // Capability defaults match spec/behavior.json hello_defaults.csharp.
            var hello = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "hello",
                ["role"] = role,
                ["worker_id"] = workerId,
                ["ts"] = _clock.Wall(),
                ["can_hijack"] = canHijack,
                // RegisterBrowser uses is_hijacked (hub internal); wire hello uses hijacked.
                ["hijacked"] = StateBool(state, "is_hijacked"),
                ["hijacked_by_me"] = StateBool(state, "hijacked_by_me"),
                ["worker_online"] = StateBool(state, "worker_online"),
                ["input_mode"] = StateStr(state, "input_mode", InputModes.Hijack),
                ["hijack_control"] = "ws",
                ["hijack_step_supported"] = true,
                ["capabilities"] = new Dictionary<string, object?>
                {
                    ["hijack_control"] = "ws",
                    ["hijack_step_supported"] = true,
                },
                ["mcp_supported"] = false, // spec/behavior.json hello_defaults.csharp
                ["vnc_supported"] = true,
                ["resume_supported"] = true,
                ["resume_token"] = resumeToken,
            });
            await conn.SendTextAsync(hello, ctx.RequestAborted).ConfigureAwait(false);
            // Per-browser owner="me"/"other" — required for second-browser tests.
            var hijackState = _deps.Hub.Router.HijackStateMsgFor(workerId, conn);
            await conn.SendTextAsync(
                ControlChannelCodec.EncodeControlFrame(hijackState),
                ctx.RequestAborted).ConfigureAwait(false);

            // DeckMux: presence_sync on join (+ fan-out when others present).
            var presenceSync = await _deckMux.OnBrowserConnectAsync(workerId, conn, role, ctx.RequestAborted)
                .ConfigureAwait(false);
            await conn.SendTextAsync(
                ControlChannelCodec.EncodeControlFrame(presenceSync),
                ctx.RequestAborted).ConfigureAwait(false);
            _deps.Hub.Conn.ActivateBrowserBroadcasts(workerId, conn);

            // One budget per connection, built here rather than on the hub: the
            // reference builds both buckets inside its WebSocket handler, and sharing
            // them per worker would let one browser starve every other viewer of the
            // same session.
            var budget = new BrowserBudget(
                new TokenBucket(_deps.Hub.BrowserRateLimitPerSec, clock: _clock),
                new TokenBucket(_deps.Hub.BrowserControlRateLimitPerSec, clock: _clock));

            while (ws.State == WebSocketState.Open)
            {
                WebSocketMessage message;
                try
                {
                    message = await WebSocketMessageReader.ReadAsync(
                        ws,
                        _deps.Hub.MaxWsMessageBytes,
                        ctx.RequestAborted,
                        _browserFragmentObserved)
                        .ConfigureAwait(false);
                }
                catch (WebSocketMessageException ex)
                {
                    await WebSocketCloseHandler.CloseAndTerminateAsync(ws, ex.CloseStatus, ex.Message)
                        .ConfigureAwait(false);
                    break;
                }

                if (message.IsClose)
                {
                    await WebSocketCloseHandler.CloseAndTerminateAsync(
                            ws,
                            message.CloseStatus ?? WebSocketCloseStatus.NormalClosure,
                            message.CloseStatusDescription)
                        .ConfigureAwait(false);
                    break;
                }
                var text = message.MessageType == WebSocketMessageType.Binary
                    && !ControlChannelCodec.IsControlFrame(message.Payload)
                        ? WsBytes.WsBytesToChannelStr(message.Payload)
                        : Encoding.UTF8.GetString(message.Payload);
                await HandleBrowserMessage(workerId, conn, role, text, budget, ctx.RequestAborted).ConfigureAwait(false);
            }
        }
        finally
        {
            var ownershipVersion = _deps.Hub.Conn.CleanupBrowser(workerId, conn);
            // Make the single-use token reclaimable before the worker resume
            // can block. A matching reclaim coordinates with that in-flight
            // resume through the hub reservation.
            FinishResumeToken(conn, ownershipVersion);
            if (ownershipVersion is not null)
            {
                _ = await _deps.Hub.Conn.ResumeWorkerIfOwnershipUnchangedAsync(
                    workerId,
                    ownershipVersion.Value,
                    new Dictionary<string, object?>
                    {
                        ["type"] = "control",
                        ["action"] = "resume",
                        ["source"] = "dashboard",
                        ["ts"] = _clock.Wall(),
                    },
                    CancellationToken.None).ConfigureAwait(false);
            }
            try
            {
                await _deckMux.OnBrowserDisconnectAsync(workerId, conn, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // best-effort
            }

            // Fan out released state when the owner drops, matching Python/Go cleanup.
            try
            {
                await _deps.Hub.BroadcastHijackStateAsync(workerId, CancellationToken.None)
                    .ConfigureAwait(false);
            }
            catch
            {
                // best-effort on disconnect
            }

            await WebSocketCloseHandler.CloseAndTerminateAsync(
                    ws, WebSocketCloseStatus.NormalClosure, "bye")
                .ConfigureAwait(false);
        }
    }

    /// <summary>A browser connection's two rate budgets, one per kind of frame.</summary>
    /// <remarks>
    /// Built per connection, as the reference builds them inside its WebSocket
    /// handler. Sharing them per worker would let one browser starve every other
    /// viewer of the same session.
    /// </remarks>
    private sealed record BrowserBudget(TokenBucket Input, TokenBucket Control);

    private async Task HandleBrowserMessage(
        string workerId, BrowserWsConn conn, string role, string text, BrowserBudget budget, CancellationToken ct)
    {
        if (!_deps.Hub.Conn.IsBrowserRegistered(workerId, conn)) return;

        if (ControlChannelCodec.IsControlFrame(text))
        {
            var dec = new ControlFrameDecoder();
            foreach (var chunk in dec.Feed(text))
            {
                if (chunk is not ControlChunk ctrl) continue;
                var mtype = ctrl.Control.TryGetValue("type", out var t) ? t?.ToString() : null;

                // Two budgets, checked before anything acts on the frame. `input`
                // is the keystroke path into somebody's terminal and gets the
                // larger allowance; every other control frame shares a tighter
                // one, so neither kind of traffic can starve the other.
                //
                // Over budget the frame is *dropped* and the caller told, not
                // disconnected: closing the socket would cost an operator their
                // session for typing quickly. Matches
                // bridge/routes/websockets_browser.py's dispatch_browser_event.
                if (mtype is not null)
                {
                    var bucket = mtype == "input" ? budget.Input : budget.Control;
                    if (!bucket.Allow())
                    {
                        _deps.Hub.Metric(
                            mtype == "input"
                                ? "ws_browser_rate_limited_total"
                                : "ws_browser_control_rate_limited_total",
                            1);
                        _deps.Hub.Log("warning", $"ws_browser_rate_limited worker_id={workerId} type={mtype}");
                        await conn.SendTextAsync(
                            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                            {
                                ["type"] = "error",
                                ["reason"] = "rate_limited",
                            }),
                            ct).ConfigureAwait(false);
                        continue;
                    }
                }

                switch (mtype)
                {
                    case "hijack_request":
                        if (role != "admin")
                        {
                            await conn.SendTextAsync(
                                ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                {
                                    ["type"] = "error",
                                    ["message"] = "Hijack requires admin role.",
                                }),
                                ct).ConfigureAwait(false);
                            break;
                        }

                        var (ok, reason) = await _deps.Hub.Lease.TryAcquireWsAsync(
                            workerId, conn, ct: ct).ConfigureAwait(false);
                        if (!ok)
                        {
                            await conn.SendTextAsync(
                                ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                {
                                    ["type"] = "error",
                                    ["message"] = reason == "already_hijacked"
                                        ? "Session already hijacked."
                                        : "Hijack failed: " + reason,
                                }),
                                ct).ConfigureAwait(false);
                            break;
                        }

                        await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        break;
                    case "hijack_release":
                        _ = await _deps.Hub.Lease.TryReleaseWsAsync(workerId, conn, ct)
                            .ConfigureAwait(false);

                        await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        break;
                    case "hijack_step":
                        if (_deps.Hub.Lease.TouchIfOwner(workerId, conn) is not null)
                        {
                            _ = await _deps.Hub.Conn.SendWorkerAsync(
                                workerId,
                                new Dictionary<string, object?>
                                {
                                    ["type"] = "control",
                                    ["action"] = "step",
                                    ["source"] = "dashboard",
                                    ["ts"] = _clock.Wall(),
                                },
                                ct).ConfigureAwait(false);
                        }
                        break;
                    case "snapshot_req":
                        break;
                    case "heartbeat":
                    {
                        // Verify and touch the exact dashboard owner atomically.
                        var exp = _deps.Hub.Lease.TouchIfOwner(workerId, conn);
                        if (exp is not null)
                        {
                            await conn.SendTextAsync(
                                ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                                {
                                    ["type"] = "heartbeat_ack",
                                    ["lease_expires_at"] = _clock.Wall()
                                        + (exp.Value - _clock.Monotonic()),
                                    ["ts"] = _clock.Wall(),
                                }),
                                ct).ConfigureAwait(false);
                            await _deps.Hub.BroadcastHijackStateAsync(workerId, ct)
                                .ConfigureAwait(false);
                        }

                        break;
                    }
                    case "resume":
                    {
                        var oldTok = ctrl.Control.TryGetValue("token", out var tokObj)
                            ? tokObj?.ToString() ?? ""
                            : "";
                        if (string.IsNullOrEmpty(oldTok))
                        {
                            break;
                        }

                        var rec = _resumeTokens.Consume(oldTok);
                        if (rec is null || rec.WorkerId != workerId) break;

                        var roleMatches = rec.Role == role;
                        var restored = false;
                        if (rec.WasDisconnected
                            && roleMatches
                            && rec.WasHijackOwner
                            && rec.OwnershipVersion is { } version)
                        {
                            (restored, _) = await _deps.Hub.Lease.TryAcquireWsAsync(
                                workerId, conn, version, ct).ConfigureAwait(false);
                        }

                        var resumed = rec.WasDisconnected
                            && roleMatches
                            && (!rec.WasHijackOwner || restored);
                        var currentTok = CurrentResumeToken(conn);
                        var newTok = resumed
                            || currentTok is null
                            || string.Equals(currentTok, oldTok, StringComparison.Ordinal)
                                ? MintResumeToken(workerId, role, conn)
                                : currentTok;
                        var stSnap = _deps.Hub.Conn.GetBrowserState(workerId, conn);
                        await conn.SendTextAsync(
                            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                            {
                                ["type"] = "hello",
                                ["role"] = role,
                                ["worker_id"] = workerId,
                                ["ts"] = _clock.Wall(),
                                ["can_hijack"] = role is "admin",
                                ["hijacked"] = stSnap.TryGetValue("is_hijacked", out var ih) && ih is true,
                                ["hijacked_by_me"] = stSnap.TryGetValue("hijacked_by_me", out var hbm) && hbm is true,
                                ["worker_online"] = stSnap.TryGetValue("worker_online", out var wo) && wo is true,
                                ["input_mode"] = stSnap.TryGetValue("input_mode", out var im) && im is string ims
                                    ? ims
                                    : InputModes.Hijack,
                                ["resume_supported"] = true,
                                ["resume_token"] = newTok,
                                ["resumed"] = resumed,
                                ["hijack_control"] = "ws",
                                ["hijack_step_supported"] = true,
                                ["mcp_supported"] = false, // spec/behavior.json hello_defaults.csharp
                                ["vnc_supported"] = true,
                            }),
                            ct).ConfigureAwait(false);
                        if (restored)
                        {
                            await _deps.Hub.BroadcastHijackStateAsync(workerId, ct).ConfigureAwait(false);
                        }
                        break;
                    }
                    case "presence_update":
                    case "control_request":
                    case "queued_input":
                        await _deckMux.HandleMessageAsync(workerId, conn, ctrl.Control, ct)
                            .ConfigureAwait(false);
                        break;
                    case "ping":
                        await conn.SendTextAsync(
                            ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
                            {
                                ["type"] = "pong",
                                ["ts"] = _clock.Wall(),
                            }),
                            ct).ConfigureAwait(false);
                        break;
                }
            }

            return;
        }

        _ = await SendBrowserInputAsync(workerId, conn, text, ct).ConfigureAwait(false);
    }

    /// <summary>Authorize and deliver one raw browser-input frame.</summary>
    internal async Task<bool> SendBrowserInputAsync(
        string workerId,
        object browser,
        string text,
        CancellationToken cancellationToken = default) =>
        await _deps.Hub.Lease.SendBrowserInputAsync(workerId, browser, text, cancellationToken)
            .ConfigureAwait(false);

    private async Task HandleWorkerWs(HttpContext ctx, string workerId)
    {
        if (!ctx.WebSockets.IsWebSocketRequest)
        {
            ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }

        if (!SafeId.IsMatch(workerId))
        {
            ctx.Response.StatusCode = StatusCodes.Status422UnprocessableEntity;
            return;
        }

        // Optional worker bearer. Refusal remains an HTTP 401 before upgrade.
        if (!WorkerBearerAuthentication.IsAuthorized(
                ctx.Request.Headers.Authorization.ToString(),
                _deps.Hub.WorkerToken))
        {
            ctx.Response.StatusCode = StatusCodes.Status401Unauthorized;
            return;
        }

        using var ws = await ctx.WebSockets.AcceptWebSocketAsync().ConfigureAwait(false);
        var conn = new BrowserWsConn(ws);
        // Seed the hub's state for a session it has not seen with that
        // session's own mode, before the registration creates it with the
        // unknown-worker default. A fresh WorkerTermState says "hijack" — the
        // reference's default too (bridge/models.py:147) — because an
        // unannounced worker is assumed to be arbitrated. But a *configured*
        // session already said what it is, and in the reference its runtime
        // announces exactly that on attach (worker_hello carries the
        // connector's input_mode). Without this, connecting a socket is enough
        // to turn a session the operator configured as `open` into one only a
        // lease holder may type at — an arbitration nobody asked for.
        var hadWorkerState = _deps.Hub.Registry.Contains(workerId);
        if (!await _deps.Hub.Conn.RegisterWorkerAsync(workerId, conn, ctx.RequestAborted)
                .ConfigureAwait(false))
        {
            await WebSocketCloseHandler.CloseAndTerminateAsync(
                    ws, WebSocketCloseStatus.PolicyViolation, "worker registration rejected")
                .ConfigureAwait(false);
            return;
        }

        if (!hadWorkerState && _deps.Registry.TryGetDefinition(workerId, out var wdef))
        {
            _deps.Hub.Registry.Get(workerId)!.InputMode = wdef.InputMode;
        }
        if (_deps.Registry is InMemorySessionRegistry mem)
        {
            // Online, and nothing else: a worker arriving is not a mode change.
            mem.MarkWorker(workerId, true, false);
        }

        // Notify already-connected browsers (Python/Go worker_connected fan-out).
        await _deps.Hub.Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "worker_connected",
                ["worker_id"] = workerId,
                ["ts"] = _clock.Wall(),
            },
            CancellationToken.None).ConfigureAwait(false);

        var decoder = new ControlFrameDecoder(new DecoderOptions
        {
            MaxControlPayloadBytes = _deps.Hub.MaxWsMessageBytes,
            MaxBufferBytes = _deps.Hub.MaxWsMessageBytes,
        });
        try
        {
            while (ws.State == WebSocketState.Open)
            {
                WebSocketMessage message;
                try
                {
                    message = await WebSocketMessageReader.ReadAsync(
                        ws,
                        _deps.Hub.MaxWsMessageBytes,
                        ctx.RequestAborted,
                        _workerFragmentObserved)
                        .ConfigureAwait(false);
                }
                catch (WebSocketMessageException ex)
                {
                    await WebSocketCloseHandler.CloseAndTerminateAsync(ws, ex.CloseStatus, ex.Message)
                        .ConfigureAwait(false);
                    break;
                }

                if (message.IsClose)
                {
                    await WebSocketCloseHandler.CloseAndTerminateAsync(
                            ws,
                            message.CloseStatus ?? WebSocketCloseStatus.NormalClosure,
                            message.CloseStatusDescription)
                        .ConfigureAwait(false);
                    break;
                }
                foreach (var chunk in decoder.FeedBytes(
                             message.Payload,
                             preserveRawData: message.MessageType == WebSocketMessageType.Binary))
                {
                    if (!await ProcessWorkerChunkAsync(workerId, conn, chunk, ctx.RequestAborted)
                            .ConfigureAwait(false))
                    {
                        return;
                    }
                }
            }
        }
        finally
        {
            await _deps.Hub.Conn.ReconcileWorkerDisconnectAsync(workerId, conn)
                .ConfigureAwait(false);

            await WebSocketCloseHandler.CloseAndTerminateAsync(
                    ws, WebSocketCloseStatus.NormalClosure, "bye")
                .ConfigureAwait(false);
        }
    }

    /// <summary>Apply one already-decoded frame from the normal worker transport.</summary>
    internal async Task<bool> ProcessWorkerChunkAsync(
        string workerId,
        IWorkerWs worker,
        Chunk chunk,
        CancellationToken cancellationToken = default)
    {
        if (chunk is ControlChunk ctrl)
        {
            var mtype = ctrl.Control.TryGetValue("type", out var t) ? t?.ToString() : null;
            bool accepted;
            if (mtype == "snapshot")
            {
                accepted = _deps.Hub.Conn.UpdateLastSnapshot(workerId, worker, ctrl.Control);
            }
            else if (mtype == "worker_hello")
            {
                // Until now every frame but `snapshot` was fanned to
                // browsers and dropped, so a worker announcing its
                // input mode was never heard — the one thing a hello
                // is for. The reference applies it here
                // (bridge/routes/websockets_worker.py), and it may
                // raise the mode but never lower a decided one.
                accepted = await ApplyWorkerHelloAsync(workerId, worker, ctrl.Control, cancellationToken)
                    .ConfigureAwait(false);
            }
            else accepted = _deps.Hub.Conn.TryAcceptWorkerFrame(workerId, worker);

            if (!accepted) return false;
            await _deps.Hub.Conn.BroadcastToBrowsersAsync(workerId, ctrl.Control, cancellationToken)
                .ConfigureAwait(false);
            return true;
        }

        if (chunk is not DataChunk data || string.IsNullOrEmpty(data.Data))
        {
            return _deps.Hub.Conn.TryAcceptWorkerFrame(workerId, worker);
        }

        // Raw terminal bytes → term control frames for every browser (Python/Go).
        if (!_deps.Hub.Conn.TryAppendWorkerEvent(
                workerId,
                worker,
                "term",
                new Dictionary<string, object?> { ["data"] = data.Data }))
        {
            return false;
        }
        await _deps.Hub.Conn.BroadcastToBrowsersAsync(
            workerId,
            new Dictionary<string, object?>
            {
                ["type"] = "term",
                ["data"] = data.Data,
                ["ts"] = _clock.Wall(),
            },
            cancellationToken).ConfigureAwait(false);
        return true;
    }

    /// <summary>WebSocket adapter implementing <see cref="IWorkerWs"/>.</summary>
    private sealed class BrowserWsConn : IAbortableBrowserWs
    {
        private readonly WebSocket _ws;
        private readonly SemaphoreSlim _sendGate = new(1, 1);

        public BrowserWsConn(WebSocket ws) => _ws = ws;

        public bool IsActive => _ws.State == WebSocketState.Open;

        public void Abort() => _ws.Abort();

        public async Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            var bytes = Encoding.UTF8.GetBytes(payload);
            await _sendGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (_ws.State != WebSocketState.Open)
                {
                    throw new WebSocketException("WebSocket is not open for send.");
                }
                await _ws.SendAsync(bytes, WebSocketMessageType.Text, true, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                _sendGate.Release();
            }
        }
    }
}

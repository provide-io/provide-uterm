//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using System.Threading.Channels;
using Provide.Uterm.Hub;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Server;

/// <summary>Why a delivery was or was not permitted at this moment.</summary>
/// <remarks>
/// Three states rather than a bool because the two refusals have opposite
/// consequences for the webhook's future; see
/// <see cref="WebhookManager.ClassifyDelivery"/>.
/// </remarks>
internal enum DeliveryVerdict
{
    /// <summary>The destination passed the guard; deliver.</summary>
    Allowed,

    /// <summary>
    /// The destination is unsafe (§1/§2/§5). Counts toward auto-unregister.
    /// </summary>
    RefusedDestination,

    /// <summary>
    /// A loopback destination on a session holding a live tunnel share (§4).
    /// Transient by nature, so it never counts toward auto-unregister.
    /// </summary>
    RefusedTunnelShare,
}

/// <summary>
/// Background-delivery wiring for <see cref="WebhookManager"/>. Every member is
/// optional, and the defaults are the ones the reference ships.
/// </summary>
public sealed class WebhookDeliveryOptions
{
    /// <summary>
    /// The event source deliveries are driven from. Subscriptions are keyed by
    /// session id, which in this port is also the worker id the hub's router
    /// publishes under.
    /// </summary>
    /// <remarks>
    /// <c>null</c> means "registered but inert" rather than an error: an embedder
    /// using the registry for its REST semantics alone has no bus, and the
    /// reference treats a missing bus the same way (its delivery task returns
    /// immediately).
    /// </remarks>
    public EventBus? EventBus { get; init; }

    /// <summary>
    /// Transport for the outbound POST. Injected so a test can assert on what was
    /// actually sent — including the signature headers — without a network.
    /// </summary>
    public HttpMessageHandler? Transport { get; init; }

    /// <summary>
    /// Delays between attempts; the count is also the number of retries. Defaults
    /// to <see cref="WebhookManager.DefaultRetryDelays"/>. An empty (non-null)
    /// list means one attempt and no retries, which is how a test reaches the
    /// give-up path without spending 3.5 seconds getting there.
    /// </summary>
    public IReadOnlyList<TimeSpan>? RetryDelays { get; init; }

    /// <summary>Per-attempt timeout; defaults to <see cref="WebhookManager.DefaultAttemptTimeout"/>.</summary>
    public TimeSpan? AttemptTimeout { get; init; }

    /// <summary>
    /// Wall clock (epoch seconds) stamped on the payload and the signature.
    /// Defaults to the real clock. The hosted factory passes the server's own
    /// clock, because a signature timestamp that disagrees with the clock the
    /// receiver's freshness window is measured against fails verification for
    /// reasons nobody can see.
    /// </summary>
    public Func<double>? Now { get; init; }

    /// <summary>Diagnostics sink, <c>(level, message)</c>. Defaults to discarding.</summary>
    public Action<string, string>? OnLog { get; init; }
}

public sealed partial class WebhookManager : IAsyncDisposable
{
    /// <summary>
    /// Delays between delivery attempts — port of the reference's
    /// <c>_RETRY_DELAYS</c>. Three retries after the first attempt, then give up.
    /// </summary>
    public static readonly IReadOnlyList<TimeSpan> DefaultRetryDelays = new[]
    {
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromSeconds(1),
        TimeSpan.FromSeconds(2),
    };

    /// <summary>Per-attempt timeout — the reference's <c>_DELIVER_TIMEOUT_S</c>.</summary>
    public static readonly TimeSpan DefaultAttemptTimeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// How many <em>consecutive</em> egress-guard refusals a webhook survives
    /// before it is unregistered — the reference's
    /// <c>_MAX_BLOCKED_DELIVERIES</c>.
    /// </summary>
    /// <remarks>
    /// Re-resolution of a name that was safe at registration can legitimately
    /// start failing; that is what DNS rebinding looks like from here. But a
    /// destination that has become permanently unsafe will never succeed, and
    /// re-evaluating it on every event means an attacker who can publish events
    /// chooses how much work the server does. Consecutive, not cumulative: a
    /// single guard pass clears the tally, so an intermittently-safe webhook is
    /// not killed by an old count.
    /// </remarks>
    public const int MaxBlockedDeliveries = 3;

    /// <summary>Counter for a webhook retired by <see cref="MaxBlockedDeliveries"/>.</summary>
    public const string AutoUnregisteredMetric = "webhook_auto_unregistered_total";

    /// <summary>Counter for one attempt the destination answered with a non-2xx.</summary>
    public const string DeliveryFailedMetric = "webhook_delivery_failed_total";

    /// <summary>Counter for an event abandoned after every attempt failed.</summary>
    public const string DeliveryGivingUpMetric = "webhook_delivery_giving_up_total";

    private static readonly JsonSerializerOptions PayloadJson = new(JsonSerializerDefaults.Web);

    private readonly Dictionary<string, DeliveryWorker> _workers = new(StringComparer.Ordinal);
    private readonly CancellationTokenSource _shutdown = new();
    private int _shutdownStarted;

    private EventBus? _eventBus;
    private HttpClient? _http;
    private IReadOnlyList<TimeSpan> _retryDelays = DefaultRetryDelays;
    private TimeSpan _attemptTimeout = DefaultAttemptTimeout;
    private Func<double> _now = WebhookSigning.WallClock;
    private Action<string, string> _log = static (_, _) => { };

    /// <summary>Apply <paramref name="options"/>, or the shipped defaults.</summary>
    private void ConfigureDelivery(WebhookDeliveryOptions? options)
    {
        _eventBus = options?.EventBus;
        _retryDelays = options?.RetryDelays ?? DefaultRetryDelays;
        _attemptTimeout = options?.AttemptTimeout ?? DefaultAttemptTimeout;
        _now = options?.Now ?? WebhookSigning.WallClock;
        _log = options?.OnLog ?? _log;

        // Built even when there is no bus: nothing will call it, and building it
        // here keeps the field non-null so every delivery path can rely on it.
        // The client-level timeout is deliberately *not* the attempt timeout —
        // each attempt is bounded by its own linked token so the bound survives a
        // caller-supplied handler that ignores HttpClient.Timeout.
        _http = options?.Transport is null
            ? new HttpClient()
            : new HttpClient(options.Transport, disposeHandler: false);
        _http.Timeout = Timeout.InfiniteTimeSpan;
    }

    /// <summary>
    /// Subscribe to the event source and start this webhook's delivery worker.
    /// Returns <c>null</c> when there is no event source to subscribe to.
    /// </summary>
    private DeliveryWorker? StartDelivery(WebhookConfig cfg)
    {
        if (_eventBus is null)
        {
            return null;
        }

        var (sub, unsubscribe) = _eventBus.Watch(cfg.SessionId, cfg.EventTypes, cfg.Pattern);
        var cancel = CancellationTokenSource.CreateLinkedTokenSource(_shutdown.Token);
        var worker = new DeliveryWorker(unsubscribe, cancel);
        worker.Loop = Task.Run(() => DeliveryLoopAsync(cfg, sub, worker, cancel.Token));
        return worker;
    }

    /// <summary>
    /// One webhook's delivery worker: drain the subscription until the
    /// worker-disconnected sentinel or teardown. Port of <c>_delivery_loop</c>.
    /// </summary>
    private async Task DeliveryLoopAsync(
        WebhookConfig cfg,
        EventBus.Subscription sub,
        DeliveryWorker worker,
        CancellationToken cancellationToken)
    {
        try
        {
            while (true)
            {
                Dictionary<string, object?>? evt;
                try
                {
                    evt = await sub.Channel.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return; // unregistered, or the manager is shutting down
                }
                catch (ChannelClosedException)
                {
                    return; // the subscription was removed under us
                }

                if (evt is null)
                {
                    return; // worker-disconnected sentinel
                }

                await DeliverAsync(cfg, evt, worker, cancellationToken).ConfigureAwait(false);
            }
        }
        catch (Exception ex)
        {
            // A worker that throws must not take the process down with it, and
            // must not vanish silently either: an unobserved task exception is
            // invisible, and "webhooks stopped working" with nothing in the log
            // is the worst possible failure mode for a fire-and-forget feature.
            _log("error", $"webhook_delivery_loop_failed webhook_id={cfg.WebhookId} error={ex.Message}");
        }
    }

    /// <summary>Run the delivery-time guard, then POST. Port of <c>_deliver</c>.</summary>
    private async Task DeliverAsync(
        WebhookConfig cfg,
        Dictionary<string, object?> evt,
        DeliveryWorker worker,
        CancellationToken cancellationToken)
    {
        switch (ClassifyDelivery(cfg))
        {
            case DeliveryVerdict.RefusedTunnelShare:
                // Counted on its own dedicated counter inside ClassifyDelivery,
                // and pointedly not counted here: a share is revocable, so this
                // refusal must not accumulate toward retiring the webhook (§4).
                _log(
                    "warn",
                    $"webhook_delivery_blocked webhook_id={cfg.WebhookId} url={cfg.Url} " +
                    $"session_id={cfg.SessionId} reason=loopback_destination_while_tunnel_shared");
                return;

            case DeliveryVerdict.RefusedDestination:
                RecordGuardBlock(cfg, worker);
                return;

            default:
                // A pass clears the consecutive-refusal tally.
                Interlocked.Exchange(ref worker.Blocked, 0);
                await PostAsync(cfg, evt, cancellationToken).ConfigureAwait(false);
                return;
        }
    }

    /// <summary>
    /// Count an egress-guard refusal, and retire the webhook once the refusals
    /// have stopped looking transient.
    /// </summary>
    private void RecordGuardBlock(WebhookConfig cfg, DeliveryWorker worker)
    {
        var count = Interlocked.Increment(ref worker.Blocked);
        _log(
            "warn",
            $"webhook_delivery_blocked webhook_id={cfg.WebhookId} url={cfg.Url} " +
            $"reason=unsafe_destination count={count}");
        if (count < MaxBlockedDeliveries)
        {
            return;
        }

        _onMetric?.Invoke(AutoUnregisteredMetric, 1);
        _log(
            "error",
            $"webhook_auto_unregistered webhook_id={cfg.WebhookId} url={cfg.Url} " +
            $"reason=ssrf_guard_threshold count={count}");
        // Safe from inside the worker: Unregister releases the subscription and
        // cancels the token without waiting for the loop to finish.
        Unregister(cfg.WebhookId);
    }

    /// <summary>POST one event, following the ported retry ladder.</summary>
    private async Task PostAsync(
        WebhookConfig cfg,
        Dictionary<string, object?> evt,
        CancellationToken cancellationToken)
    {
        var payload = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["webhook_id"] = cfg.WebhookId,
            ["session_id"] = cfg.SessionId,
            ["event"] = evt,
            ["timestamp"] = _now(),
        };
        var body = JsonSerializer.SerializeToUtf8Bytes(payload, PayloadJson);

        // Signed once, not once per attempt: a retry is the same delivery, and
        // re-stamping it would hand the receiver's replay window a fresh
        // timestamp for a body it may already have seen.
        string? timestamp = null;
        string? signature = null;
        if (!string.IsNullOrEmpty(cfg.Secret))
        {
            timestamp = WebhookSigning.FormatTimestamp(_now());
            signature = WebhookSigning.BuildWebhookSignature(cfg.Secret, body, timestamp);
        }

        for (var attempt = 0; ; attempt++)
        {
            if (await AttemptAsync(cfg, body, timestamp, signature, attempt, cancellationToken)
                    .ConfigureAwait(false))
            {
                return;
            }

            if (attempt >= _retryDelays.Count)
            {
                break;
            }

            try
            {
                await Task.Delay(_retryDelays[attempt], cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }

        _onMetric?.Invoke(DeliveryGivingUpMetric, 1);
        _log("error", $"webhook_delivery_giving_up webhook_id={cfg.WebhookId} url={cfg.Url}");
    }

    /// <summary>One POST. True when the destination answered 2xx.</summary>
    private async Task<bool> AttemptAsync(
        WebhookConfig cfg,
        byte[] body,
        string? timestamp,
        string? signature,
        int attempt,
        CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, cfg.Url)
        {
            Content = new ByteArrayContent(body),
        };
        request.Content.Headers.ContentType =
            new System.Net.Http.Headers.MediaTypeHeaderValue("application/json");
        if (timestamp is not null && signature is not null)
        {
            // Header names are fixed by the receiving side of the contract —
            // auth_webhook.py:191-192 — so they are not ours to rename.
            request.Headers.TryAddWithoutValidation("X-Uterm-Timestamp", timestamp);
            request.Headers.TryAddWithoutValidation("X-Uterm-Signature", signature);
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(_attemptTimeout);
        try
        {
            using var response = await _http!
                .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token)
                .ConfigureAwait(false);
            if (response.IsSuccessStatusCode)
            {
                return true;
            }

            _onMetric?.Invoke(DeliveryFailedMetric, 1);
            _log(
                "warn",
                $"webhook_delivery_failed webhook_id={cfg.WebhookId} url={cfg.Url} " +
                $"status={(int)response.StatusCode} attempt={attempt + 1}");
            return false;
        }
        catch (Exception ex)
        {
            // Deliberately broad, and deliberately not counted as a *failed*
            // delivery: the reference distinguishes a destination that answered
            // badly (counted) from one that could not be reached at all (logged),
            // and only the retry ladder and the give-up counter speak for the
            // latter. Broad because every transport fault — DNS, refused
            // connection, TLS, the attempt timeout — is the same decision here:
            // log it, and let the ladder decide whether to try again.
            _log(
                "warn",
                $"webhook_delivery_error webhook_id={cfg.WebhookId} url={cfg.Url} " +
                $"error={ex.Message} attempt={attempt + 1}");
            return false;
        }
    }

    /// <summary>
    /// Release every delivery worker and clear the registry, then wait for the
    /// workers to exit — so a caller can be sure nothing will POST after this
    /// returns.
    /// </summary>
    /// <remarks>
    /// Never call this from inside a delivery worker: it awaits them.
    /// <see cref="Unregister"/> is the one that is safe from in there.
    /// </remarks>
    public async Task ShutdownAsync()
    {
        // Idempotent: the server shuts the registry down from DisposeAsync, a host
        // may well shut it down itself, and cancelling an already-disposed token
        // source throws — so a double shutdown would turn orderly teardown into an
        // exception on the way out the door.
        if (Interlocked.Exchange(ref _shutdownStarted, 1) != 0)
        {
            return;
        }

        List<DeliveryWorker> workers;
        lock (_gate)
        {
            workers = _workers.Values.ToList();
            _workers.Clear();
            _webhooks.Clear();
        }

        await _shutdown.CancelAsync().ConfigureAwait(false);
        foreach (var worker in workers)
        {
            worker.Release();
        }

        // Faults are already logged by the loop itself; this wait is about
        // quiescence, not about learning how each worker ended.
        await Task.WhenAll(workers.Select(w => w.Loop)).ConfigureAwait(false);

        _http?.Dispose();
        _shutdown.Dispose();
    }

    /// <inheritdoc />
    public async ValueTask DisposeAsync() => await ShutdownAsync().ConfigureAwait(false);

    /// <summary>One webhook's worker: its subscription, its token, its tally.</summary>
    private sealed class DeliveryWorker
    {
        private readonly Action _unsubscribe;
        private readonly CancellationTokenSource _cancel;
        private int _released;

        internal DeliveryWorker(Action unsubscribe, CancellationTokenSource cancel)
        {
            _unsubscribe = unsubscribe;
            _cancel = cancel;
        }

        internal Task Loop { get; set; } = Task.CompletedTask;

        /// <summary>Consecutive egress-guard refusals; see <see cref="MaxBlockedDeliveries"/>.</summary>
        internal int Blocked;

        /// <summary>
        /// Tear down the subscription and release the loop. Idempotent, because
        /// unregister-then-shutdown is an ordinary sequence and cancelling a
        /// disposed token source throws.
        /// </summary>
        internal void Release()
        {
            if (Interlocked.Exchange(ref _released, 1) != 0)
            {
                return;
            }

            _unsubscribe();
            _cancel.Cancel();
            _cancel.Dispose();
        }
    }
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Server;

/// <summary>Registered webhook config (REST surface; delivery is optional).</summary>
public sealed class WebhookConfig
{
    public string WebhookId { get; set; } = "";
    public string SessionId { get; set; } = "";
    public string Url { get; set; } = "";
    public List<string>? EventTypes { get; set; }
    public string? Pattern { get; set; }
    public string? Secret { get; set; }
}

/// <summary>
/// In-memory webhook registry with one background delivery worker per registered
/// webhook (port of Python <c>WebhookManager</c> / Go <c>WebhookRegistry</c>).
/// </summary>
/// <remarks>
/// <para>
/// The destination guard lives in <see cref="WebhookEgressPolicy"/> and
/// implements <c>conformance/EGRESS_GUARD.md</c>. What used to be here was a
/// single <c>uri.IsLoopback</c> test, which meant <c>169.254.169.254</c> and any
/// <c>10.x</c> admin port were reachable destinations regardless of
/// configuration.
/// </para>
/// <para>
/// Delivery lives in <c>Webhooks.Delivery.cs</c>. It was absent from this port
/// entirely — "does not run background delivery (host can wire later)" — which
/// had two consequences worth naming: webhooks were a REST surface that recorded
/// registrations and then did nothing with them, and the delivery-time half of
/// the egress contract (§4, and re-classification against DNS rebinding) was
/// fully implemented with no caller, so it protected nothing.
/// </para>
/// </remarks>
public sealed partial class WebhookManager
{
    /// <summary>
    /// Counter for any delivery the egress guard refuses. Named for parity with
    /// the reference's <c>webhook_delivery_blocked_total</c>.
    /// </summary>
    public const string DeliveryBlockedMetric = "webhook_delivery_blocked_total";

    /// <summary>
    /// Counter for the §4 refusal specifically — a loopback destination on a
    /// session that currently holds a live tunnel share. Separate from
    /// <see cref="DeliveryBlockedMetric"/> because this one is not a
    /// misconfiguration an operator can read off the config file: it depends on
    /// a share issued at runtime, so it needs to be attributable on its own.
    /// </summary>
    /// <remarks>
    /// The name is pinned by <c>conformance/EGRESS_GUARD.md</c> §4, not chosen
    /// here. Three ports each invented a spelling of it
    /// (<c>..._tunnel_share_total</c> was this port's), which is a counter an
    /// operator cannot alert on across a fleet: the same refusal would arrive
    /// under a different key depending on which language served the session.
    /// </remarks>
    public const string DeliveryBlockedTunnelMetric = "webhook_delivery_blocked_tunnel_total";

    private readonly object _gate = new();
    private readonly Dictionary<string, WebhookConfig> _webhooks = new(StringComparer.Ordinal);
    private readonly WebhookEgressPolicy _egress;
    private readonly Func<string, bool> _tunnelShareActive;
    private readonly Action<string, long>? _onMetric;

    /// <param name="allowLoopbackDestinations">
    /// The <em>effective</em> permission (§3), not the raw config key — callers
    /// building from config should pass
    /// <see cref="WebhookEgressPolicy.EffectiveAllowLoopbackDestinations"/>.
    /// Defaults to <c>false</c>, matching the reference's
    /// <c>allow_loopback_destinations: bool = False</c>. It defaulted to
    /// <c>true</c> here, so an embedder that never thought about egress got the
    /// permissive posture — the opposite of what a guard should do when unasked.
    /// </param>
    /// <param name="resolver">
    /// DNS for destinations named by hostname. Injected so tests are hermetic;
    /// defaults to the platform resolver.
    /// </param>
    /// <param name="tunnelShareActive">
    /// Whether the given session holds a live tunnel share <em>right now</em>
    /// (§4). Defaults to "no", which is the correct answer for an embedder with
    /// no tunnel store; the hosted factory passes one backed by the real store.
    /// </param>
    /// <param name="onMetric">Counter sink; see the constants above.</param>
    /// <param name="delivery">
    /// Background-delivery wiring. <c>null</c> — and an options object with no
    /// <see cref="WebhookDeliveryOptions.EventBus"/> — registers webhooks that
    /// never fire, which is the graceful no-op an embedder with no event source
    /// gets (matching the reference's <c>event_bus=None</c> branch).
    /// </param>
    public WebhookManager(
        bool allowLoopbackDestinations = false,
        IHostResolver? resolver = null,
        Func<string, bool>? tunnelShareActive = null,
        Action<string, long>? onMetric = null,
        WebhookDeliveryOptions? delivery = null)
    {
        _egress = new WebhookEgressPolicy(allowLoopbackDestinations, resolver);
        _tunnelShareActive = tunnelShareActive ?? (_ => false);
        _onMetric = onMetric;
        ConfigureDelivery(delivery);
    }

    /// <summary>
    /// Validate a destination at registration time. Refusals surface as
    /// <see cref="ArgumentException"/>, which the REST route turns into a 422
    /// carrying the guard's own reason.
    /// </summary>
    public void ValidateUrl(string url)
    {
        var decision = _egress.Classify(url);
        if (!decision.Allowed)
        {
            throw new ArgumentException(decision.Reason);
        }
    }

    /// <summary>
    /// May this webhook be delivered <em>now</em>? The delivery-time half of the
    /// guard (§4).
    /// </summary>
    /// <remarks>
    /// <para>
    /// Two things can only be decided here rather than at registration. The
    /// first is re-classification: a hostname that resolved to a public address
    /// when it was registered can answer with the metadata IP later, and only a
    /// check at delivery sees that.
    /// </para>
    /// <para>
    /// The second is the tunnel share. Tunnel sharing exposes a loopback-bound
    /// server through a relay, so "bound to loopback" stops implying "only local
    /// callers exist" — and therefore stops justifying loopback destinations.
    /// Shares are issued at runtime (<c>POST /api/tunnels</c>), so at config-load
    /// time the fact is neither true nor false yet, which is exactly why this
    /// cannot be folded into the effective permission. An expired share does not
    /// keep the guard closed: the question asked is whether a share is live now.
    /// </para>
    /// </remarks>
    public bool IsDeliveryAllowed(WebhookConfig cfg) => ClassifyDelivery(cfg) == DeliveryVerdict.Allowed;

    /// <summary>
    /// The same question as <see cref="IsDeliveryAllowed"/>, but reporting
    /// <em>which</em> refusal happened.
    /// </summary>
    /// <remarks>
    /// The delivery worker cannot act on a bare bool: the two refusals have
    /// opposite consequences. An unsafe destination accumulates toward the
    /// three-strike auto-unregister, because a destination that has gone bad will
    /// not come back and re-evaluating it forever burns CPU on an attacker's
    /// schedule. A tunnel-share refusal must not, because the share is revocable
    /// and the webhook is expected to resume the moment it is (§4).
    /// </remarks>
    internal DeliveryVerdict ClassifyDelivery(WebhookConfig cfg)
    {
        var decision = _egress.Classify(cfg.Url);
        if (!decision.Allowed)
        {
            _onMetric?.Invoke(DeliveryBlockedMetric, 1);
            return DeliveryVerdict.RefusedDestination;
        }

        if (decision.Loopback && _tunnelShareActive(cfg.SessionId))
        {
            // The dedicated counter and *only* the dedicated counter (§4). The
            // generic one feeds the three-strike auto-unregister below, and a
            // tunnel share is revocable at any moment: a few minutes of sharing
            // would otherwise permanently delete a webhook whose destination was
            // never wrong. This refusal is not a verdict on the destination — it
            // says "not while you are sharing" — so it must not accumulate
            // toward retiring it. The reference makes the same split
            // (webhooks.py `_refuse_loopback_while_tunnel_shared`, which
            // deliberately does not touch `_blocked_counts`), and so does Go
            // (`server_webhooks.go` deliver/recordGuardBlock).
            _onMetric?.Invoke(DeliveryBlockedTunnelMetric, 1);
            return DeliveryVerdict.RefusedTunnelShare;
        }

        return DeliveryVerdict.Allowed;
    }

    public void ValidatePattern(string? pattern)
    {
        if (string.IsNullOrEmpty(pattern))
        {
            return;
        }

        if (pattern.Length > 200)
        {
            throw new ArgumentException("pattern exceeds max length 200");
        }

        try
        {
            _ = new Regex(pattern, RegexOptions.Compiled, TimeSpan.FromSeconds(1));
        }
        catch (Exception ex)
        {
            // RegexParseException derives from ArgumentException on .NET; wrap so
            // callers always see ArgumentException (API parity with URL validation).
            throw new ArgumentException("invalid pattern: " + ex.Message, ex);
        }
    }

    public WebhookConfig Register(
        string sessionId,
        string url,
        IReadOnlyList<string>? eventTypes,
        string? pattern,
        string? secret)
    {
        ValidateUrl(url);
        ValidatePattern(pattern);
        var cfg = new WebhookConfig
        {
            WebhookId = Guid.NewGuid().ToString("N"),
            SessionId = sessionId,
            Url = url,
            EventTypes = eventTypes is null ? null : eventTypes.ToList(),
            Pattern = pattern,
            Secret = secret,
        };
        // Subscribed before the caller is told the webhook exists, rather than
        // inside the worker task the way the reference does it. Once Register has
        // returned, an event published on the very next line has to be seen —
        // otherwise there is a window in which a webhook the API says is
        // registered silently misses events, and its width depends on how fast
        // the task scheduler gets around to the worker.
        var worker = StartDelivery(cfg);
        lock (_gate)
        {
            _webhooks[cfg.WebhookId] = cfg;
            if (worker is not null)
            {
                _workers[cfg.WebhookId] = worker;
            }
        }

        return cfg;
    }

    public IReadOnlyList<WebhookConfig> ListWebhooks(string sessionId)
    {
        lock (_gate)
        {
            return _webhooks.Values.Where(w => w.SessionId == sessionId).ToList();
        }
    }

    public WebhookConfig? GetWebhook(string webhookId)
    {
        lock (_gate)
        {
            return _webhooks.TryGetValue(webhookId, out var w) ? w : null;
        }
    }

    /// <summary>
    /// Drop a webhook and release its delivery worker.
    /// </summary>
    /// <remarks>
    /// Deliberately does not wait for the worker to finish, so it is safe to call
    /// from inside that worker — which the auto-unregister path does. (The
    /// reference has to schedule a task to avoid awaiting its own delivery task;
    /// nothing here awaits, so it does not need to.)
    /// </remarks>
    public bool Unregister(string webhookId)
    {
        DeliveryWorker? worker;
        bool removed;
        lock (_gate)
        {
            removed = _webhooks.Remove(webhookId);
            _workers.Remove(webhookId, out worker);
        }

        worker?.Release();
        return removed;
    }
}

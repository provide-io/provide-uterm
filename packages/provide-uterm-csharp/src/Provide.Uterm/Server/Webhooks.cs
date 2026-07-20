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
/// In-memory webhook registry (Python WebhookManager register/list/unregister).
/// Validates URL/pattern; does not run background delivery (host can wire later).
/// </summary>
public sealed class WebhookManager
{
    private readonly object _gate = new();
    private readonly Dictionary<string, WebhookConfig> _webhooks = new(StringComparer.Ordinal);
    private readonly bool _allowLoopback;

    public WebhookManager(bool allowLoopbackDestinations = true)
    {
        // Default true for local/dev TEST_MODE; production hosts can pass false.
        _allowLoopback = allowLoopbackDestinations;
    }

    public void ValidateUrl(string url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            throw new ArgumentException("url is required");
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
            (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new ArgumentException("url must be absolute http(s)");
        }

        if (!_allowLoopback &&
            (uri.IsLoopback ||
             string.Equals(uri.Host, "localhost", StringComparison.OrdinalIgnoreCase)))
        {
            throw new ArgumentException("loopback webhook destinations are not allowed");
        }
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

        _ = new Regex(pattern, RegexOptions.Compiled, TimeSpan.FromSeconds(1));
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
        lock (_gate)
        {
            _webhooks[cfg.WebhookId] = cfg;
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

    public bool Unregister(string webhookId)
    {
        lock (_gate)
        {
            return _webhooks.Remove(webhookId);
        }
    }
}

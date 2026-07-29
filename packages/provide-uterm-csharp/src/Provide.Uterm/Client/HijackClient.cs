//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Client;

/// <summary>API error from a non-2xx hijack/session response.</summary>
public sealed class ApiException : Exception
{
    public int StatusCode { get; }
    public object? Body { get; }

    public ApiException(int statusCode, string message, object? body = null)
        : base(message)
    {
        StatusCode = statusCode;
        Body = body;
    }
}

/// <summary>
/// REST client for the provide-uterm hijack + session API.
/// Paths match packages/provide-uterm-go/client and the C# UtermServer routes.
/// </summary>
public sealed class HijackClient : IDisposable
{
    private static readonly Regex SafeIdPattern = new(@"^[A-Za-z0-9._-]+$", RegexOptions.Compiled);

    private readonly string _baseUrl;
    private readonly string _entityPrefix;
    private readonly TimeSpan _timeout;
    private readonly Dictionary<string, string> _headers;
    private readonly HttpClient _http;
    private readonly bool _ownsHttp;

    public const int DefaultLeaseS = 90;
    public static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(20);
    public const string DefaultEntityPrefix = "/worker";

    public HijackClient(
        string baseUrl,
        string? entityPrefix = null,
        TimeSpan? timeout = null,
        IReadOnlyDictionary<string, string>? headers = null,
        HttpClient? httpClient = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _entityPrefix = (entityPrefix ?? DefaultEntityPrefix).TrimEnd('/');
        _timeout = timeout ?? DefaultTimeout;
        _headers = headers is null
            ? new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            : new Dictionary<string, string>(headers, StringComparer.OrdinalIgnoreCase);
        _ownsHttp = httpClient is null;
        _http = httpClient ?? new HttpClient();
    }

    public static HijackClient WithBearer(string baseUrl, string token, string? entityPrefix = null) =>
        new(baseUrl, entityPrefix, headers: new Dictionary<string, string>
        {
            ["Authorization"] = "Bearer " + token,
        });

    /// <summary>Alias for <see cref="WithBearer"/>.</summary>
    public static HijackClient CreateWithBearer(string baseUrl, string token, string? entityPrefix = null) =>
        WithBearer(baseUrl, token, entityPrefix);

    private static string SafeId(string value, string kind)
    {
        if (string.IsNullOrEmpty(value) || value is "." or ".." || !SafeIdPattern.IsMatch(value))
        {
            throw new ArgumentException($"invalid {kind}: \"{value}\"");
        }

        return value;
    }

    private string Wp(string workerId) => _entityPrefix + "/" + SafeId(workerId, "worker_id");

    private string Hp(string workerId, string hijackId) =>
        Wp(workerId) + "/hijack/" + SafeId(hijackId, "hijack_id");

    private string Sp(string sessionId) => "/api/sessions/" + SafeId(sessionId, "session_id");

    public Task<Dictionary<string, object?>> AcquireAsync(
        string workerId, string owner = "operator", int leaseS = DefaultLeaseS, CancellationToken ct = default)
    {
        if (string.IsNullOrEmpty(owner)) owner = "operator";
        if (leaseS <= 0) leaseS = DefaultLeaseS;
        return RequestObjectAsync(HttpMethod.Post, Wp(workerId) + "/hijack/acquire",
            new Dictionary<string, object?> { ["owner"] = owner, ["lease_s"] = leaseS }, ct);
    }

    // Sync-named aliases used by some call sites
    public Task<Dictionary<string, object?>> Acquire(string workerId, string owner = "operator", int leaseS = DefaultLeaseS, CancellationToken ct = default) =>
        AcquireAsync(workerId, owner, leaseS, ct);

    /// <summary>Acquire with an arbitrary request body (MCP / advanced callers).</summary>
    public Task<Dictionary<string, object?>> Acquire(
        string workerId, IReadOnlyDictionary<string, object?> body, CancellationToken ct = default)
    {
        var owner = body.TryGetValue("owner", out var o) && o is string s && s.Length > 0 ? s : "operator";
        var leaseS = body.TryGetValue("lease_s", out var l) ? Convert.ToInt32(l) : DefaultLeaseS;
        return AcquireAsync(workerId, owner, leaseS, ct);
    }

    public Task<Dictionary<string, object?>> HeartbeatAsync(
        string workerId, string hijackId, int leaseS = DefaultLeaseS, CancellationToken ct = default)
    {
        if (leaseS <= 0) leaseS = DefaultLeaseS;
        return RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/heartbeat",
            new Dictionary<string, object?> { ["lease_s"] = leaseS }, ct);
    }

    public Task<Dictionary<string, object?>> Heartbeat(string workerId, string hijackId, int leaseS = DefaultLeaseS, CancellationToken ct = default) =>
        HeartbeatAsync(workerId, hijackId, leaseS, ct);

    public Task<Dictionary<string, object?>> Heartbeat(string workerId, string hijackId, CancellationToken ct) =>
        HeartbeatAsync(workerId, hijackId, DefaultLeaseS, ct);

    public Task<Dictionary<string, object?>> SendAsync(
        string workerId, string hijackId, string keys, string? expectPromptId = null, string? expectRegex = null, int timeoutMs = 2000, int pollIntervalMs = 120, CancellationToken ct = default)
    {
        var body = new Dictionary<string, object?>
        {
            ["keys"] = keys,
            ["timeout_ms"] = timeoutMs,
            ["poll_interval_ms"] = pollIntervalMs,
        };
        if (expectPromptId != null) body["expect_prompt_id"] = expectPromptId;
        if (expectRegex != null) body["expect_regex"] = expectRegex;
        return RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/send", body, ct);
    }

    public Task<Dictionary<string, object?>> Send(string workerId, string hijackId, string keys, string? expectPromptId = null, string? expectRegex = null, int timeoutMs = 2000, int pollIntervalMs = 120, CancellationToken ct = default) =>
        SendAsync(workerId, hijackId, keys, expectPromptId, expectRegex, timeoutMs, pollIntervalMs, ct);

    public Task<Dictionary<string, object?>> StepAsync(string workerId, string hijackId, int steps = 1, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/step",
            new Dictionary<string, object?> { ["steps"] = steps }, ct);

    public Task<Dictionary<string, object?>> Step(string workerId, string hijackId, CancellationToken ct = default) =>
        StepAsync(workerId, hijackId, 1, ct);

    public Task<Dictionary<string, object?>> Step(string workerId, string hijackId, int steps, CancellationToken ct = default) =>
        StepAsync(workerId, hijackId, steps, ct);

    public Task<Dictionary<string, object?>> ReleaseAsync(string workerId, string hijackId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/release", null, ct);

    public Task<Dictionary<string, object?>> Release(string workerId, string hijackId, CancellationToken ct = default) =>
        ReleaseAsync(workerId, hijackId, ct);

    public Task<Dictionary<string, object?>> SnapshotAsync(
        string workerId, string hijackId, int waitMs = 1500, CancellationToken ct = default)
    {
        var q = "?wait_ms=" + (waitMs <= 0 ? 1500 : waitMs);
        return RequestObjectAsync(HttpMethod.Get, Hp(workerId, hijackId) + "/snapshot" + q, null, ct);
    }

    public Task<Dictionary<string, object?>> Snapshot(string workerId, string hijackId, int waitMs = 1500, CancellationToken ct = default) =>
        SnapshotAsync(workerId, hijackId, waitMs, ct);

    /// <summary>Worker-level snapshot (no hijack id) — used by MCP hijack_read.</summary>
    public Task<Dictionary<string, object?>> Snapshot(string workerId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, Wp(workerId) + "/snapshot", null, ct);

    public Task<Dictionary<string, object?>> EventsAsync(
        string workerId, string hijackId, int afterSeq = 0, int limit = 200, CancellationToken ct = default)
    {
        var q = $"?after_seq={afterSeq}&limit={(limit <= 0 ? 200 : limit)}";
        return RequestObjectAsync(HttpMethod.Get, Hp(workerId, hijackId) + "/events" + q, null, ct);
    }

    public Task<Dictionary<string, object?>> Events(string workerId, string hijackId, int afterSeq = 0, int limit = 200, CancellationToken ct = default) =>
        EventsAsync(workerId, hijackId, afterSeq, limit, ct);

    /// <summary>Worker-level events (no hijack id) — used by MCP hijack_read / session_watch.</summary>
    public Task<Dictionary<string, object?>> Events(string workerId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, Wp(workerId) + "/events", null, ct);

    public Task<Dictionary<string, object?>> SetInputModeAsync(string workerId, string mode, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Wp(workerId) + "/input_mode",
            new Dictionary<string, object?> { ["input_mode"] = mode }, ct);

    public Task<Dictionary<string, object?>> SetInputMode(string workerId, string mode, CancellationToken ct = default) =>
        SetInputModeAsync(workerId, mode, ct);

    public Task<Dictionary<string, object?>> DisconnectWorkerAsync(string workerId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Wp(workerId) + "/disconnect_worker", null, ct);

    public Task<Dictionary<string, object?>> DisconnectWorker(string workerId, CancellationToken ct = default) =>
        DisconnectWorkerAsync(workerId, ct);

    public Task<Dictionary<string, object?>> HealthAsync(CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, "/api/health", null, ct);

    /// <summary>Attach a graphical session (C# v1: mode=memory fixture; RFB later).</summary>
    public Task<Dictionary<string, object?>> GuiAttachAsync(
        string workerId,
        IReadOnlyDictionary<string, object?>? body = null,
        CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Wp(workerId) + "/gui/attach",
            body is null
                ? new Dictionary<string, object?> { ["mode"] = "memory" }
                : new Dictionary<string, object?>(body), ct);

    public Task<Dictionary<string, object?>> GuiScreenshotAsync(
        string workerId, string hijackId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, Hp(workerId, hijackId) + "/gui/screenshot", null, ct);

    public Task<Dictionary<string, object?>> GuiClickAsync(
        string workerId, string hijackId, int x, int y, string button = "left", CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/gui/click",
            new Dictionary<string, object?> { ["x"] = x, ["y"] = y, ["button"] = button }, ct);

    public Task<Dictionary<string, object?>> GuiTypeAsync(
        string workerId, string hijackId, string text, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/gui/type",
            new Dictionary<string, object?> { ["text"] = text }, ct);

    public Task<Dictionary<string, object?>> GuiKeyAsync(
        string workerId, string hijackId, string keyName, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/gui/key",
            new Dictionary<string, object?> { ["key_name"] = keyName }, ct);

    public Task<Dictionary<string, object?>> GuiDragAsync(
        string workerId, string hijackId, int startX, int startY, int endX, int endY, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Hp(workerId, hijackId) + "/gui/drag",
            new Dictionary<string, object?>
            {
                ["start_x"] = startX,
                ["start_y"] = startY,
                ["end_x"] = endX,
                ["end_y"] = endY,
            }, ct);

    public Task<Dictionary<string, object?>> Health(CancellationToken ct = default) => HealthAsync(ct);

    /// <summary>
    /// The sessions the server listed, handed back as it sent them.
    ///
    /// <c>GET /api/sessions</c> answers a bare JSON array, so this returns one —
    /// the same as Python's <c>list_sessions</c> and Go's
    /// <c>ListSessions() (any, error)</c>. It used to be declared as a
    /// dictionary, which an array cannot be, so it arrived wrapped in
    /// <c>{"sessions": [...]}</c> and a caller of this client saw a different
    /// shape from a caller of any other port's client against the same server.
    /// </summary>
    public Task<object?> ListSessionsAsync(CancellationToken ct = default) =>
        RequestAnyAsync(HttpMethod.Get, "/api/sessions", null, ct);

    public Task<object?> ListSessions(CancellationToken ct = default) => ListSessionsAsync(ct);

    public Task<Dictionary<string, object?>> GetSessionAsync(string sessionId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, Sp(sessionId), null, ct);

    public Task<Dictionary<string, object?>> GetSession(string sessionId, CancellationToken ct = default) =>
        GetSessionAsync(sessionId, ct);

    /// <summary>
    /// The session's last snapshot, or null when nothing has been drawn yet —
    /// the endpoint answers <c>dict | None</c> (Python <c>session_snapshot</c>),
    /// and a dictionary cannot be a null, so this is not one.
    /// </summary>
    public Task<object?> SessionSnapshot(string sessionId, CancellationToken ct = default) =>
        RequestAnyAsync(HttpMethod.Get, Sp(sessionId) + "/snapshot", null, ct);

    /// <summary>
    /// The session's recent events, as the bare JSON array the endpoint answers
    /// (Python <c>list[dict]</c>, Go <c>SessionEvents() (any, error)</c>).
    /// </summary>
    public Task<object?> SessionEvents(string sessionId, CancellationToken ct = default) =>
        RequestAnyAsync(HttpMethod.Get, Sp(sessionId) + "/events", null, ct);

    public Task<Dictionary<string, object?>> WatchSessionEvents(string sessionId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Get, Sp(sessionId) + "/events/watch", null, ct);

    public Task<Dictionary<string, object?>> SetSessionMode(string sessionId, string mode, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Sp(sessionId) + "/mode",
            new Dictionary<string, object?> { ["mode"] = mode }, ct);

    public Task<Dictionary<string, object?>> ConnectSession(
        string sessionId, object? body = null, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Sp(sessionId) + "/connect",
            body as Dictionary<string, object?> ?? new Dictionary<string, object?>(), ct);

    public Task<Dictionary<string, object?>> DisconnectSession(string sessionId, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, Sp(sessionId) + "/disconnect", null, ct);

    public Task<Dictionary<string, object?>> QuickConnect(object body, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, "/api/sessions/quick-connect",
            body as Dictionary<string, object?> ?? new Dictionary<string, object?> { ["value"] = body }, ct);

    public Task<Dictionary<string, object?>> Post(string path, object? body = null, CancellationToken ct = default) =>
        RequestObjectAsync(HttpMethod.Post, path,
            body as Dictionary<string, object?> ?? (body is null ? null : new Dictionary<string, object?> { ["value"] = body }), ct);

    private async Task<Dictionary<string, object?>> RequestObjectAsync(
        HttpMethod method, string path, Dictionary<string, object?>? body, CancellationToken ct)
    {
        var any = await RequestAnyAsync(method, path, body, ct).ConfigureAwait(false);
        return any as Dictionary<string, object?>
               ?? new Dictionary<string, object?> { ["value"] = any };
    }

    private async Task<object?> RequestAnyAsync(
        HttpMethod method, string path, Dictionary<string, object?>? body, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(_timeout);
        using var req = new HttpRequestMessage(method, _baseUrl + path);
        req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        foreach (var (k, v) in _headers)
        {
            req.Headers.TryAddWithoutValidation(k, v);
        }

        if (body is not null)
        {
            req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        }

        using var resp = await _http.SendAsync(req, cts.Token).ConfigureAwait(false);
        var raw = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
        object? parsed = null;
        if (!string.IsNullOrWhiteSpace(raw))
        {
            try
            {
                using var doc = JsonDocument.Parse(raw);
                // One decoding for every JSON shape: an object becomes a
                // dictionary, an array a list, and a scalar itself. Decoding
                // only objects and handing back the undecoded bytes for
                // everything else made a null body arrive as the string "null".
                parsed = JsonElementToObject(doc.RootElement.Clone());
            }
            catch
            {
                parsed = raw;
            }
        }

        if (!resp.IsSuccessStatusCode)
        {
            throw new ApiException((int)resp.StatusCode, $"HTTP {(int)resp.StatusCode} {method} {path}", parsed);
        }

        return parsed;
    }

    private static Dictionary<string, object?> JsonElementToDict(JsonElement el)
    {
        var d = new Dictionary<string, object?>();
        foreach (var prop in el.EnumerateObject())
        {
            d[prop.Name] = JsonElementToObject(prop.Value);
        }

        return d;
    }

    private static object? JsonElementToObject(JsonElement el) => el.ValueKind switch
    {
        JsonValueKind.Object => JsonElementToDict(el),
        JsonValueKind.Array => el.EnumerateArray().Select(JsonElementToObject).ToList(),
        JsonValueKind.String => el.GetString(),
        JsonValueKind.Number when el.TryGetInt64(out var l) => l,
        JsonValueKind.Number => el.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        _ => el.ToString(),
    };

    public void Dispose()
    {
        if (_ownsHttp) _http.Dispose();
    }
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Http.Headers;
using System.Text.Json;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.ServerAuth;

/// <summary>
/// Authorization provider that delegates every decision to an external webhook
/// (Go WebhookAuthorizationProvider parity). When <see cref="Secret"/> is non-empty,
/// responses must carry a valid X-Uterm-Signature (fail closed on unsigned allow).
/// </summary>
public sealed class WebhookAuthorizationProvider : IAuthorizationProvider, IDisposable
{
    private readonly HttpClient _http;
    private readonly bool _ownsHttp;

    public string Url { get; }
    public string Secret { get; }
    public double TimeoutS { get; }
    public bool RequireSignedResponse { get; }

    /// <summary>Injectable clock (Unix seconds). Defaults to wall clock.</summary>
    public Func<double> Now { get; set; } = WebhookSigning.WallClock;

    public WebhookAuthorizationProvider(string url, string secret, double timeoutS = 2.0, HttpClient? httpClient = null)
    {
        Url = url;
        Secret = secret ?? "";
        TimeoutS = timeoutS <= 0 ? 2.0 : timeoutS;
        RequireSignedResponse = !string.IsNullOrWhiteSpace(Secret);
        if (httpClient is null)
        {
            _http = new HttpClient { Timeout = TimeSpan.FromSeconds(TimeoutS) };
            _ownsHttp = true;
        }
        else
        {
            _http = httpClient;
            _ownsHttp = false;
        }
    }

    public void Dispose()
    {
        if (_ownsHttp)
        {
            _http.Dispose();
        }
    }

    public bool HasCapability(Principal p, string capability) => Check(p, capability, null);

    public bool IsAdmin(Principal p) => Check(p, "admin", null);

    public bool IsOwner(Principal p, SessionDefinition session) =>
        Check(p, "session.owner", ContextSession(session));

    public bool CanReadSession(Principal p, SessionDefinition session) =>
        Check(p, "session.read", ContextSession(session));

    public bool CanReadRecording(Principal p, SessionDefinition session) =>
        Check(p, "session.recording.read", ContextSession(session));

    public bool CanCreateSession(Principal p) => Check(p, "session.control.create", null);

    public bool CanMutateSession(Principal p, SessionDefinition session, string action) =>
        Check(p, action, ContextSession(session));

    public StringSet CapabilitiesFor(Principal p)
    {
        if (p is null)
        {
            return new StringSet();
        }

        try
        {
            var payload = new Dictionary<string, object?>
            {
                ["subject_id"] = p.SubjectId,
                ["action"] = "capabilities",
            };
            var body = JsonSerializer.SerializeToUtf8Bytes(payload);
            var raw = Post(body);
            if (raw is null)
            {
                return new StringSet();
            }

            using var doc = JsonDocument.Parse(raw);
            if (!doc.RootElement.TryGetProperty("capabilities", out var caps) || caps.ValueKind != JsonValueKind.Array)
            {
                return new StringSet();
            }

            var set = new StringSet();
            foreach (var item in caps.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                {
                    var s = item.GetString();
                    if (!string.IsNullOrEmpty(s)) set.Add(s);
                }
            }

            return set;
        }
        catch (Exception)
        {
            return new StringSet();
        }
    }

    public string ResolveBrowserRole(Principal p, SessionDefinition session)
    {
        if (p is null)
        {
            return "viewer";
        }

        try
        {
            var payload = new Dictionary<string, object?>
            {
                ["principal"] = new Dictionary<string, object?>
                {
                    ["subject_id"] = p.SubjectId,
                    ["roles"] = p.Roles.ToList(),
                },
                ["session_id"] = session.SessionId,
                ["action"] = "resolve_role",
            };
            var body = JsonSerializer.SerializeToUtf8Bytes(payload);
            var raw = Post(body);
            if (raw is null)
            {
                return "viewer";
            }

            using var doc = JsonDocument.Parse(raw);
            if (!doc.RootElement.TryGetProperty("role", out var roleEl) || roleEl.ValueKind != JsonValueKind.String)
            {
                return "viewer";
            }

            var role = (roleEl.GetString() ?? "").Trim().ToLowerInvariant();
            return role is "admin" or "operator" or "viewer" ? role : "viewer";
        }
        catch (Exception)
        {
            return "viewer";
        }
    }

    public override string ToString() => $"WebhookAuthorizationProvider({Url})";

    private static Dictionary<string, object?> ContextSession(SessionDefinition session) =>
        new() { ["session_id"] = session.SessionId };

    private bool Check(Principal? p, string action, Dictionary<string, object?>? extra)
    {
        if (p is null)
        {
            return false;
        }

        extra ??= new Dictionary<string, object?>();
        try
        {
            var payload = new Dictionary<string, object?>
            {
                ["principal"] = new Dictionary<string, object?>
                {
                    ["subject_id"] = p.SubjectId,
                    ["roles"] = p.Roles.ToList(),
                    ["scopes"] = p.Scopes.ToList(),
                    ["claims"] = p.Claims,
                },
                ["action"] = action,
                ["context"] = extra,
            };
            var body = JsonSerializer.SerializeToUtf8Bytes(payload);
            var raw = Post(body);
            if (raw is null)
            {
                return false;
            }

            using var doc = JsonDocument.Parse(raw);
            return doc.RootElement.TryGetProperty("allow", out var allow) && allow.ValueKind == JsonValueKind.True;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private byte[]? Post(byte[] body)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, Url)
            {
                Content = new ByteArrayContent(body),
            };
            req.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
            ApplySignedHeaders(req, body);

            // Prefer SendAsync: custom HttpMessageHandler stubs typically only override
            // SendAsync; the sync Send path throws NotSupportedException on base handlers.
            using var resp = _http.SendAsync(req).ConfigureAwait(false).GetAwaiter().GetResult();
            if ((int)resp.StatusCode != 200)
            {
                return null;
            }

            using var stream = resp.Content.ReadAsStreamAsync().ConfigureAwait(false).GetAwaiter().GetResult();
            using var ms = new MemoryStream();
            // Cap at 1 MiB like Go (io.LimitReader 1<<20).
            var buffer = new byte[8192];
            long total = 0;
            int n;
            while ((n = stream.Read(buffer, 0, buffer.Length)) > 0)
            {
                total += n;
                if (total > 1 << 20)
                {
                    return null;
                }

                ms.Write(buffer, 0, n);
            }

            var raw = ms.ToArray();
            if (!ResponseSignatureOk(raw, resp.Headers))
            {
                return null;
            }

            return raw;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private void ApplySignedHeaders(HttpRequestMessage req, byte[] body)
    {
        if (string.IsNullOrWhiteSpace(Secret))
        {
            return;
        }

        var ts = WebhookSigning.FormatTimestamp(Now());
        req.Headers.TryAddWithoutValidation("X-Uterm-Timestamp", ts);
        req.Headers.TryAddWithoutValidation("X-Uterm-Signature", WebhookSigning.BuildWebhookSignature(Secret, body, ts));
    }

    private bool ResponseSignatureOk(byte[] body, HttpResponseHeaders headers)
    {
        if (!RequireSignedResponse)
        {
            return true;
        }

        headers.TryGetValues("X-Uterm-Signature", out var sigVals);
        headers.TryGetValues("X-Uterm-Timestamp", out var tsVals);
        var sig = sigVals?.FirstOrDefault() ?? "";
        var ts = tsVals?.FirstOrDefault() ?? "";
        return WebhookSigning.VerifyWebhookSignature(Secret, body, sig, ts, WebhookSigning.DefaultMaxAgeS, Now());
    }
}

//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;

namespace Provide.Uterm.TunnelClient;

/// <summary>
/// Browser HTTP intercept gate helpers. Port of
/// packages/provide-uterm-go/tunnelclient/intercept.go pure surfaces
/// (SanitizeHeaders / ParseActionMessage / InterceptGate).
/// </summary>
public sealed class InterceptDecision
{
    public string Action { get; init; } = "forward"; // forward | drop | modify
    public Dictionary<string, string>? Headers { get; init; }
    public byte[]? Body { get; init; }
}

public static class InterceptHeaders
{
    /// <summary>
    /// Headers an operator-controlled browser MUST NOT inject into a forwarded
    /// request (hop-by-hop, framing, identity/authority).
    /// </summary>
    private static readonly HashSet<string> Denylisted = new(StringComparer.OrdinalIgnoreCase)
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
        "content-length",
        "host", "authorization", "cookie", "forwarded",
        "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
    };

    public static (Dictionary<string, string> Cleaned, List<string> Dropped) SanitizeHeaders(
        IReadOnlyDictionary<string, string> raw)
    {
        var cleaned = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var dropped = new List<string>();
        foreach (var (k, v) in raw)
        {
            if (Denylisted.Contains(k))
            {
                dropped.Add(k);
                continue;
            }

            cleaned[k] = v;
        }

        dropped.Sort(StringComparer.Ordinal);
        return (cleaned, dropped);
    }

    /// <summary>
    /// Parse an http_action message from the browser into an InterceptDecision.
    /// Unknown actions fall back to "forward"; invalid body_b64 is ignored.
    /// </summary>
    public static InterceptDecision ParseActionMessage(IReadOnlyDictionary<string, object?> msg)
    {
        var action = "forward";
        if (msg.TryGetValue("action", out var a) && a is string s)
        {
            action = s;
        }

        if (action is not ("forward" or "drop" or "modify"))
        {
            action = "forward";
        }

        var d = new InterceptDecision { Action = action };
        if (action != "modify")
        {
            return d;
        }

        Dictionary<string, string>? headers = null;
        if (msg.TryGetValue("headers", out var rawHeaders))
        {
            var src = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            switch (rawHeaders)
            {
                case IReadOnlyDictionary<string, object?> map:
                    foreach (var (k, v) in map)
                    {
                        src[k] = StringifyHeaderValue(v);
                    }

                    break;
                case IReadOnlyDictionary<string, string> smap:
                    foreach (var (k, v) in smap)
                    {
                        src[k] = v;
                    }

                    break;
            }

            if (src.Count > 0)
            {
                var (cleaned, _) = SanitizeHeaders(src);
                headers = cleaned;
            }
        }

        byte[]? body = null;
        if (msg.TryGetValue("body_b64", out var b64Obj) && b64Obj is string b64)
        {
            try
            {
                body = Convert.FromBase64String(b64);
            }
            catch (FormatException)
            {
                // invalid base64 ignored
            }
        }

        return new InterceptDecision { Action = action, Headers = headers, Body = body };
    }

    internal static string StringifyHeaderValue(object? v) =>
        v switch
        {
            null => "",
            string s => s,
            bool b => b ? "True" : "False",
            double d when d == Math.Truncate(d) => ((long)d).ToString(CultureInfo.InvariantCulture),
            float f when f == Math.Truncate(f) => ((long)f).ToString(CultureInfo.InvariantCulture),
            IFormattable f => f.ToString(null, CultureInfo.InvariantCulture) ?? "",
            _ => v.ToString() ?? "",
        };
}

/// <summary>
/// Manages pending intercepted HTTP requests (pause-before-forward).
/// Port of Go InterceptGate pure surface.
/// </summary>
public sealed class InterceptGate
{
    private readonly object _gate = new();
    private readonly Dictionary<string, TaskCompletionSource<InterceptDecision>> _pending = new();
    private bool _enabled;
    private bool _inspectEnabled = true;
    private readonly double _timeoutS;
    private readonly string _timeoutAction;

    public InterceptGate(double timeoutS = 30, string timeoutAction = "forward")
    {
        _timeoutS = timeoutS < 1.0 ? 1.0 : timeoutS;
        _timeoutAction = timeoutAction is "forward" or "drop" ? timeoutAction : "forward";
    }

    public double TimeoutS => _timeoutS;
    public string TimeoutAction => _timeoutAction;

    public bool Enabled
    {
        get { lock (_gate) return _enabled; }
        set { lock (_gate) _enabled = value; }
    }

    public bool InspectEnabled
    {
        get { lock (_gate) return _inspectEnabled; }
        set { lock (_gate) _inspectEnabled = value; }
    }

    public string RegisterPending(string requestId)
    {
        lock (_gate)
        {
            _pending[requestId] = new TaskCompletionSource<InterceptDecision>(
                TaskCreationOptions.RunContinuationsAsynchronously);
        }

        return requestId;
    }

    public bool Resolve(string requestId, InterceptDecision decision)
    {
        TaskCompletionSource<InterceptDecision>? tcs;
        lock (_gate)
        {
            if (!_pending.Remove(requestId, out tcs))
            {
                return false;
            }
        }

        return tcs.TrySetResult(decision);
    }

    public async Task<InterceptDecision> AwaitDecisionAsync(
        string requestId,
        CancellationToken cancellationToken = default)
    {
        TaskCompletionSource<InterceptDecision> tcs;
        lock (_gate)
        {
            if (!_pending.TryGetValue(requestId, out tcs!))
            {
                return new InterceptDecision { Action = _timeoutAction };
            }
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(TimeSpan.FromSeconds(_timeoutS));
        try
        {
            return await tcs.Task.WaitAsync(cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            lock (_gate)
            {
                _pending.Remove(requestId);
            }

            return new InterceptDecision { Action = _timeoutAction };
        }
    }
}

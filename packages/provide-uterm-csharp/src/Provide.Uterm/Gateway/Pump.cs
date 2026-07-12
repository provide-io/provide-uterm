//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Gateway;

/// <summary>
/// Gateway control-frame helpers shared by telnet/SSH pumps.
/// Port of packages/provide-uterm-go/gateway/pump.go pure surfaces.
/// </summary>
public static class GatewayPump
{
    /// <summary>
    /// Capability hello the gateways send upstream on every (re)connect.
    /// </summary>
    public static Dictionary<string, object?> HelloFrame() => new()
    {
        ["type"] = "hello",
        ["v"] = 1,
        ["features"] = new List<object?> { "supports_redirect" },
    };

    /// <summary>In-memory resume token (+ optional player id).</summary>
    public sealed class TokenRec
    {
        public required string Token { get; init; }
        public long? PlayerId { get; init; }
    }

    /// <summary>Mutable per-connection control-channel state.</summary>
    public sealed class ControlState
    {
        public TokenRec? Token { get; set; }
        public string? Redirect { get; set; }
    }

    /// <summary>
    /// Intercept a gateway control frame. Returns true when the frame was a
    /// recognised gateway control message.
    /// </summary>
    public static bool HandleControlFrame(
        IReadOnlyDictionary<string, object?> frame,
        ControlState st,
        Action<byte[]>? writeClient = null)
    {
        if (!frame.TryGetValue("type", out var typeObj) || typeObj is not string msgType)
        {
            return false;
        }

        switch (msgType)
        {
            case "session_token":
            {
                if (!frame.TryGetValue("token", out var tok) || tok is null)
                {
                    return false;
                }

                long? pid = null;
                if (frame.TryGetValue("player_id", out var pidObj))
                {
                    pid = AsInt64(pidObj);
                }

                st.Token = new TokenRec { Token = tok.ToString() ?? "", PlayerId = pid };
                return true;
            }
            case "resume_ok":
                writeClient?.Invoke(System.Text.Encoding.UTF8.GetBytes("\r\n[Session resumed]\r\n"));
                return true;
            case "resume_failed":
                st.Token = null;
                return true;
            case "redirect":
                if (frame.TryGetValue("path", out var path) && path is string p)
                {
                    st.Redirect = p;
                    return true;
                }

                return false;
            default:
                return false;
        }
    }

    /// <summary>Build a resume control message from the held token.</summary>
    public static Dictionary<string, object?> ResumeFrame(TokenRec t)
    {
        var m = new Dictionary<string, object?> { ["type"] = "resume", ["token"] = t.Token };
        if (t.PlayerId is { } pid)
        {
            m["player_id"] = pid;
        }

        return m;
    }

    private static long? AsInt64(object? v) =>
        v switch
        {
            long l => l,
            int i => i,
            double d => (long)d,
            string s when long.TryParse(s, out var x) => x,
            _ => null,
        };
}

/// <summary>
/// Loopback bind policy for unauthenticated telnet. Port of Go
/// isLoopbackBindHost / AllowUnauthenticated gate.
/// </summary>
public static class GatewayBindPolicy
{
    public static bool IsLoopbackBindHost(string host)
    {
        if (string.IsNullOrEmpty(host))
        {
            return false;
        }

        if (host is "127.0.0.1" or "::1" or "localhost" or "0.0.0.0" or "::")
        {
            // 0.0.0.0 / :: are not loopback — reject (fail-closed).
            return host is "127.0.0.1" or "::1" or "localhost";
        }

        return System.Net.IPAddress.TryParse(host, out var ip) && System.Net.IPAddress.IsLoopback(ip);
    }

    /// <summary>
    /// Throws when binding unauthenticated telnet to a non-loopback host without
    /// explicit allow.
    /// </summary>
    public static void RequireUnauthenticatedAllowed(string host, bool allowUnauthenticated)
    {
        if (allowUnauthenticated || IsLoopbackBindHost(host))
        {
            return;
        }

        throw new InvalidOperationException(
            "refusing to start an unauthenticated telnet gateway on a non-loopback bind address; " +
            "set AllowUnauthenticated only when this listener is protected by another access-control layer");
    }
}

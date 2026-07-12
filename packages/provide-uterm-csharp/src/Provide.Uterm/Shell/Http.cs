//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Shell;

/// <summary>HTTP helpers for fetch/cast/render. Port of Go shell/http.go + fetchBytes.</summary>
internal static class ShellHttp
{
    public static async Task<(int Status, byte[] Data)> DoHttpAsync(
        HttpClient client,
        string method,
        string url,
        string? body,
        TimeSpan timeout,
        int maxRead,
        string userAgent,
        CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        using var req = new HttpRequestMessage(new HttpMethod(method), url);
        if (body is not null)
        {
            req.Content = new StringContent(body, Encoding.UTF8);
        }

        if (!string.IsNullOrEmpty(userAgent))
        {
            req.Headers.UserAgent.ParseAdd(userAgent);
        }

        using var resp = await client.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token)
            .ConfigureAwait(false);
        await using var stream = await resp.Content.ReadAsStreamAsync(cts.Token).ConfigureAwait(false);
        if (maxRead > 0)
        {
            var buf = new byte[maxRead];
            var total = 0;
            while (total < maxRead)
            {
                var r = await stream.ReadAsync(buf.AsMemory(total, maxRead - total), cts.Token).ConfigureAwait(false);
                if (r == 0)
                {
                    break;
                }

                total += r;
            }

            return ((int)resp.StatusCode, buf[..total]);
        }

        using var ms = new MemoryStream();
        await stream.CopyToAsync(ms, cts.Token).ConfigureAwait(false);
        return ((int)resp.StatusCode, ms.ToArray());
    }

    public static async Task<(byte[]? Data, ShellResult Err, bool Ok)> FetchBytesAsync(
        HttpClient client, string url, CancellationToken ct)
    {
        if (url.StartsWith("file://", StringComparison.Ordinal))
        {
            var path = url["file://".Length..];
            if (!File.Exists(path) || Directory.Exists(path))
            {
                return (null, ShellResult.OfText(ShellOutput.ErrorMsg("file not found: " + path) + ShellOutput.Prompt), false);
            }

            try
            {
                var data = await File.ReadAllBytesAsync(path, ct).ConfigureAwait(false);
                return (data, ShellResult.OfText(), true);
            }
            catch (Exception ex)
            {
                return (null, ShellResult.OfText(ShellOutput.ErrorMsg("cannot fetch: " + ex.Message) + ShellOutput.Prompt), false);
            }
        }

        if (url.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
            url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                var (_, data) = await DoHttpAsync(client, "GET", url, null, TimeSpan.FromSeconds(30), 0, "provide-uterm/1.0", ct)
                    .ConfigureAwait(false);
                return (data, ShellResult.OfText(), true);
            }
            catch (Exception ex)
            {
                return (null, ShellResult.OfText(ShellOutput.ErrorMsg("cannot fetch: " + ex.Message) + ShellOutput.Prompt), false);
            }
        }

        return (null, ShellResult.OfText(
            ShellOutput.ErrorMsg("unsupported URL scheme (use http://, https://, or file://)") + ShellOutput.Prompt), false);
    }

    public static async Task<(string Text, ShellResult Err, bool Ok)> FetchTextAsync(
        HttpClient client, string url, CancellationToken ct)
    {
        var (data, err, ok) = await FetchBytesAsync(client, url, ct).ConfigureAwait(false);
        if (!ok || data is null)
        {
            return ("", err, false);
        }

        return (Encoding.UTF8.GetString(data), ShellResult.OfText(), true);
    }
}

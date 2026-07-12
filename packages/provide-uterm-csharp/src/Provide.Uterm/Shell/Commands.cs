//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.Shell;

/// <summary>Ushell command implementations. Port of Go shell/cmd_*.go.</summary>
internal static class Commands
{
    public const string KvPrefix = "session:";

    public static ShellResult CmdPy(string source)
    {
        if (source.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("usage: py <expr>") + ShellOutput.Prompt);
        }

        return ShellResult.OfText(
            ShellOutput.ErrorMsg("py: unavailable in the Go build (Python sandbox not ported)") + ShellOutput.Prompt);
    }

    public static async Task<ShellResult> CmdSessionsAsync(ShellContext? c, CancellationToken ct)
    {
        if (c?.ListKvSessions is null)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("list_kv_sessions not available in this context") + ShellOutput.Prompt);
        }

        IReadOnlyList<IReadOnlyDictionary<string, object?>> sessions;
        try
        {
            sessions = await c.ListKvSessions(ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(ex.Message) + ShellOutput.Prompt);
        }

        if (sessions.Count == 0)
        {
            return ShellResult.OfText(ShellOutput.InfoMsg("no sessions found") + ShellOutput.Prompt);
        }

        var rows = new List<IReadOnlyList<string>>();
        foreach (var s in sessions)
        {
            var status = StrUtil.Truthy(s.TryGetValue("connected", out var conn) ? conn : null) ? "live" : "idle";
            rows.Add(new[]
            {
                StrUtil.StrOrDefault(s, "session_id", "?"),
                StrUtil.StrOrDefault(s, "lifecycle_state", "?"),
                StrUtil.StrOrDefault(s, "connector_type", "?"),
                status,
            });
        }

        var table = ShellOutput.FmtTable(rows, new[] { "session_id", "state", "type", "status" });
        return ShellResult.OfText(table + ShellOutput.Prompt);
    }

    public static async Task<ShellResult> CmdSessionsKillAsync(ShellContext? c, string sessionId, CancellationToken ct)
    {
        if (sessionId.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("usage: sessions kill <session_id>") + ShellOutput.Prompt);
        }

        var ns = c?.Env?.Runtime();
        if (ns is null)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("SESSION_RUNTIME DO binding not available") + ShellOutput.Prompt);
        }

        try
        {
            await ns.KillAsync(sessionId, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(ex.Message) + ShellOutput.Prompt);
        }

        return ShellResult.OfText(ShellOutput.SuccessMsg("kill signal sent to " + sessionId) + ShellOutput.Prompt);
    }

    public static async Task<ShellResult> CmdKvAsync(ShellContext? c, string arg, CancellationToken ct)
    {
        var kv = c?.Env?.Registry();
        if (kv is null)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("SESSION_REGISTRY KV binding not available") + ShellOutput.Prompt);
        }

        var subParts = StrUtil.PySplit1(arg);
        var sub = subParts is { Length: > 0 } ? subParts[0].ToLowerInvariant() : "";
        var keyArg = subParts is { Length: > 1 } ? StrUtil.PyStrip(subParts[1]) : "";

        try
        {
            switch (sub)
            {
                case "list":
                {
                    var names = await kv.ListAsync(KvPrefix, ct).ConfigureAwait(false);
                    var kept = names.Where(n => n.Length > 0).Select(n => "  " + ShellOutput.Cyan + n + ShellOutput.Reset).ToList();
                    if (kept.Count == 0)
                    {
                        return ShellResult.OfText(ShellOutput.InfoMsg("no keys found") + ShellOutput.Prompt);
                    }

                    return ShellResult.OfText(string.Join("\r\n", kept) + "\r\n" + ShellOutput.Prompt);
                }
                case "get":
                {
                    if (keyArg.Length == 0)
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("usage: kv get <key>") + ShellOutput.Prompt);
                    }

                    var fullKey = WithPrefix(keyArg);
                    var value = await kv.GetAsync(fullKey, ct).ConfigureAwait(false);
                    if (value is null)
                    {
                        return ShellResult.OfText(ShellOutput.InfoMsg("key not found: " + fullKey) + ShellOutput.Prompt);
                    }

                    return ShellResult.OfText(ShellOutput.Dim + fullKey + ShellOutput.Reset + "\r\n" + value + "\r\n" + ShellOutput.Prompt);
                }
                case "set":
                {
                    if (keyArg.Length == 0)
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("usage: kv set <key> <value>") + ShellOutput.Prompt);
                    }

                    var kvParts = StrUtil.PySplit1(keyArg);
                    if (kvParts is null || kvParts.Length < 2)
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("usage: kv set <key> <value>") + ShellOutput.Prompt);
                    }

                    var fullKey = WithPrefix(kvParts[0]);
                    await kv.PutAsync(fullKey, kvParts[1], ct).ConfigureAwait(false);
                    return ShellResult.OfText(ShellOutput.SuccessMsg("set " + fullKey) + ShellOutput.Prompt);
                }
                case "delete":
                {
                    if (keyArg.Length == 0)
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("usage: kv delete <key>") + ShellOutput.Prompt);
                    }

                    var fullKey = WithPrefix(keyArg);
                    await kv.DeleteAsync(fullKey, ct).ConfigureAwait(false);
                    return ShellResult.OfText(ShellOutput.SuccessMsg("deleted " + fullKey) + ShellOutput.Prompt);
                }
                default:
                    return ShellResult.OfText(
                        ShellOutput.ErrorMsg("usage: kv list | kv get <key> | kv set <key> <value> | kv delete <key>") +
                        ShellOutput.Prompt);
            }
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(ex.Message) + ShellOutput.Prompt);
        }
    }

    public static async Task<ShellResult> CmdStorageAsync(ShellContext? c, string arg, CancellationToken ct)
    {
        if (c?.Storage is null)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("storage not available in this context") + ShellOutput.Prompt);
        }

        var storage = c.Storage;
        var subParts = StrUtil.PySplit1(arg);
        var sub = subParts is { Length: > 0 } ? subParts[0].ToLowerInvariant() : "";
        var keyArg = subParts is { Length: > 1 } ? StrUtil.PyStrip(subParts[1]) : "";

        try
        {
            switch (sub)
            {
                case "list":
                {
                    var keys = await storage.ListAsync(ct).ConfigureAwait(false);
                    var kept = keys.Where(k => k.Length > 0).Select(k => "  " + ShellOutput.Cyan + k + ShellOutput.Reset).ToList();
                    if (kept.Count == 0)
                    {
                        return ShellResult.OfText(ShellOutput.InfoMsg("no storage keys found") + ShellOutput.Prompt);
                    }

                    return ShellResult.OfText(string.Join("\r\n", kept) + "\r\n" + ShellOutput.Prompt);
                }
                case "get":
                {
                    if (keyArg.Length == 0)
                    {
                        return ShellResult.OfText(ShellOutput.ErrorMsg("usage: storage get <key>") + ShellOutput.Prompt);
                    }

                    var value = await storage.GetAsync(keyArg, ct).ConfigureAwait(false);
                    if (value is null)
                    {
                        return ShellResult.OfText(ShellOutput.InfoMsg("key not found: " + keyArg) + ShellOutput.Prompt);
                    }

                    return ShellResult.OfText(ShellOutput.Dim + keyArg + ShellOutput.Reset + "\r\n" + value + "\r\n" + ShellOutput.Prompt);
                }
                default:
                    return ShellResult.OfText(ShellOutput.ErrorMsg("usage: storage list | storage get <key>") + ShellOutput.Prompt);
            }
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(ex.Message) + ShellOutput.Prompt);
        }
    }

    public static async Task<ShellResult> CmdFetchAsync(HttpClient client, string arg, CancellationToken ct)
    {
        if (arg.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + ShellOutput.Prompt);
        }

        var method = "GET";
        var rest = arg;
        if (rest == "-X" || rest.StartsWith("-X ", StringComparison.Ordinal) || rest.StartsWith("-X\t", StringComparison.Ordinal))
        {
            var parts = StrUtil.PySplit1(rest[2..]);
            if (parts is null || parts.Length == 0)
            {
                return ShellResult.OfText(ShellOutput.ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + ShellOutput.Prompt);
            }

            method = parts[0].ToUpperInvariant();
            rest = parts.Length > 1 ? parts[1] : "";
        }

        var urlBody = StrUtil.PySplit1(rest);
        var url = urlBody is { Length: > 0 } ? urlBody[0] : "";
        string? body = urlBody is { Length: > 1 } ? urlBody[1] : null;
        if (url.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("usage: fetch [-X METHOD] <url> [body]") + ShellOutput.Prompt);
        }

        try
        {
            var (status, data) = await ShellHttp.DoHttpAsync(
                client, method, url, body, TimeSpan.FromSeconds(10), 4096, "", ct).ConfigureAwait(false);
            var text = Encoding.UTF8.GetString(data);
            var runes = text.EnumerateRunes().ToList();
            var previewRunes = runes.Count > 800 ? runes.Take(800) : runes;
            var preview = string.Concat(previewRunes).Replace("\n", "\r\n", StringComparison.Ordinal);
            var truncated = runes.Count > 800 ? " …" : "";
            var color = status >= 500 ? ShellOutput.Red : status >= 400 ? ShellOutput.Yellow : ShellOutput.Green;
            return ShellResult.OfText(
                $"{color}HTTP {status}{ShellOutput.Reset}\r\n{preview}{truncated}\r\n" + ShellOutput.Prompt);
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(ex.Message) + ShellOutput.Prompt);
        }
    }

    public static async Task<ShellResult> CmdRenderAsync(
        HttpClient client,
        Func<byte[], int, int, string, (IReadOnlyList<string> Frames, double Fps)>? renderImage,
        string arg,
        CancellationToken ct)
    {
        const string usage = "usage: render [--mode truecolor|256|16] [--cols N] [--rows N] [--fps N] [--loop] <url>";
        if (arg.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(usage) + ShellOutput.Prompt);
        }

        var mode = "truecolor";
        var cols = 80;
        var rows = 24;
        var loop = false;
        double? fpsOverride = null;
        var url = "";
        var tokens = StrUtil.PyFields(arg);
        for (var i = 0; i < tokens.Length;)
        {
            var tok = tokens[i];
            if (tok == "--mode" && i + 1 < tokens.Length)
            {
                var raw = tokens[i + 1];
                if (raw is not ("truecolor" or "256" or "16"))
                {
                    return ShellResult.OfText(
                        ShellOutput.ErrorMsg("unknown mode '" + raw + "' (use truecolor, 256, or 16)") + ShellOutput.Prompt);
                }

                mode = raw;
                i += 2;
            }
            else if (tok == "--cols" && i + 1 < tokens.Length)
            {
                if (!int.TryParse(tokens[i + 1], out cols))
                {
                    return ShellResult.OfText(ShellOutput.ErrorMsg("invalid --cols value: " + tokens[i + 1]) + ShellOutput.Prompt);
                }

                i += 2;
            }
            else if (tok == "--rows" && i + 1 < tokens.Length)
            {
                if (!int.TryParse(tokens[i + 1], out rows))
                {
                    return ShellResult.OfText(ShellOutput.ErrorMsg("invalid --rows value: " + tokens[i + 1]) + ShellOutput.Prompt);
                }

                i += 2;
            }
            else if (tok == "--fps" && i + 1 < tokens.Length)
            {
                if (!double.TryParse(tokens[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out var f))
                {
                    return ShellResult.OfText(ShellOutput.ErrorMsg("invalid --fps value: " + tokens[i + 1]) + ShellOutput.Prompt);
                }

                fpsOverride = f;
                i += 2;
            }
            else if (tok == "--loop")
            {
                loop = true;
                i++;
            }
            else if (!tok.StartsWith("--", StringComparison.Ordinal))
            {
                url = tok;
                i++;
            }
            else
            {
                return ShellResult.OfText(ShellOutput.ErrorMsg("unknown flag: " + tok) + ShellOutput.Prompt);
            }
        }

        if (url.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg(usage) + ShellOutput.Prompt);
        }

        var (data, errRes, ok) = await ShellHttp.FetchBytesAsync(client, url, ct).ConfigureAwait(false);
        if (!ok || data is null)
        {
            return errRes;
        }

        if (renderImage is null)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("cannot decode image: no renderer configured") + ShellOutput.Prompt);
        }

        try
        {
            var (frames, sourceFps) = renderImage(data, cols, rows, mode);
            var fpsFinal = fpsOverride ?? sourceFps;
            if (frames.Count <= 1 || fpsFinal <= 0)
            {
                if (frames.Count > 0)
                {
                    return ShellResult.OfText(frames[0] + ShellOutput.Prompt);
                }

                return ShellResult.OfText(ShellOutput.ErrorMsg("empty image") + ShellOutput.Prompt);
            }

            return ShellResult.OfAnimated(new AnimatedResult { Frames = frames.ToList(), Fps = fpsFinal, Loop = loop });
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("cannot decode image: " + ex.Message) + ShellOutput.Prompt);
        }
    }

    public static async Task<ShellResult> CmdCastAsync(HttpClient client, string arg, CancellationToken ct)
    {
        var tokens = StrUtil.PyFields(arg);
        var url = "";
        var loop = false;
        double? fpsOverride = null;
        for (var i = 0; i < tokens.Length;)
        {
            var tok = tokens[i];
            if (tok == "--loop")
            {
                loop = true;
                i++;
            }
            else if (tok == "--fps" && i + 1 < tokens.Length)
            {
                if (!double.TryParse(tokens[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out var f))
                {
                    return ShellResult.OfText(ShellOutput.ErrorMsg("invalid --fps value: " + tokens[i + 1]) + ShellOutput.Prompt);
                }

                fpsOverride = f;
                i += 2;
            }
            else if (!tok.StartsWith("--", StringComparison.Ordinal))
            {
                url = tok;
                i++;
            }
            else
            {
                return ShellResult.OfText(ShellOutput.ErrorMsg("unknown flag: " + tok) + ShellOutput.Prompt);
            }
        }

        if (url.Length == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("usage: cast [--fps N] [--loop] <url>") + ShellOutput.Prompt);
        }

        var (text, errRes, ok) = await ShellHttp.FetchTextAsync(client, url, ct).ConfigureAwait(false);
        if (!ok)
        {
            return errRes;
        }

        var rawLines = NonBlankLines(text);
        if (rawLines.Count == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("empty cast file") + ShellOutput.Prompt);
        }

        JsonElement header;
        try
        {
            header = JsonSerializer.Deserialize<JsonElement>(rawLines[0]);
        }
        catch (Exception ex)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("invalid cast header: " + ex.Message) + ShellOutput.Prompt);
        }

        if (header.ValueKind != JsonValueKind.Object)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("invalid cast header: header is not an object") + ShellOutput.Prompt);
        }

        if (!header.TryGetProperty("version", out var verEl) || !VersionIsTwo(verEl))
        {
            var vStr = header.TryGetProperty("version", out var v) ? PyValue(v) : "None";
            return ShellResult.OfText(ShellOutput.ErrorMsg("unsupported asciicast version: " + vStr) + ShellOutput.Prompt);
        }

        var events = new List<(double Ts, string Data)>();
        for (var li = 1; li < rawLines.Count; li++)
        {
            try
            {
                using var doc = JsonDocument.Parse(rawLines[li]);
                var root = doc.RootElement;
                if (root.ValueKind != JsonValueKind.Array || root.GetArrayLength() < 3)
                {
                    continue;
                }

                var arr = root.EnumerateArray().ToArray();
                if (arr[1].ToString() != "o")
                {
                    continue;
                }

                if (!TryToFloat(arr[0], out var ts))
                {
                    continue;
                }

                events.Add((ts, arr[2].ToString()));
            }
            catch
            {
                // skip bad lines
            }
        }

        if (events.Count == 0)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("no output events in cast file") + ShellOutput.Prompt);
        }

        var targetFps = fpsOverride ?? 15.0;
        var frameDur = 1.0 / targetFps;
        var totalDur = events[^1].Ts + frameDur;
        var nFrames = Math.Max(1, (int)(totalDur / frameDur));
        var buckets = new string[nFrames];
        Array.Fill(buckets, "");
        foreach (var e in events)
        {
            var idx = (int)(e.Ts / frameDur);
            if (idx > nFrames - 1)
            {
                idx = nFrames - 1;
            }

            if (idx < 0)
            {
                idx = 0;
            }

            buckets[idx] += e.Data;
        }

        var frames = new List<string> { ShellOutput.ClearScreen };
        var started = false;
        foreach (var bucket in buckets)
        {
            if (bucket.Length > 0 || started)
            {
                started = true;
                frames.Add(bucket);
            }
        }

        if (frames.Count <= 1)
        {
            return ShellResult.OfText(ShellOutput.ErrorMsg("cast file has no displayable output") + ShellOutput.Prompt);
        }

        return ShellResult.OfAnimated(new AnimatedResult { Frames = frames, Fps = targetFps, Loop = loop });
    }

    private static string WithPrefix(string key) =>
        key.StartsWith(KvPrefix, StringComparison.Ordinal) ? key : KvPrefix + key;

    private static List<string> NonBlankLines(string text)
    {
        var normalized = text.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n');
        return normalized.Split('\n').Where(ln => StrUtil.PyStrip(ln).Length > 0).ToList();
    }

    private static bool VersionIsTwo(JsonElement v) =>
        TryToFloat(v, out var f) && Math.Abs(f - 2) < 1e-9;

    private static bool TryToFloat(JsonElement v, out double f)
    {
        f = 0;
        switch (v.ValueKind)
        {
            case JsonValueKind.Number:
                f = v.GetDouble();
                return true;
            case JsonValueKind.String:
                return double.TryParse(v.GetString(), NumberStyles.Float, CultureInfo.InvariantCulture, out f);
            default:
                return false;
        }
    }

    private static string PyValue(JsonElement v) => v.ValueKind switch
    {
        JsonValueKind.Null => "None",
        JsonValueKind.Number when v.TryGetInt64(out var i) => i.ToString(CultureInfo.InvariantCulture),
        JsonValueKind.Number => v.GetDouble().ToString(CultureInfo.InvariantCulture),
        _ => v.ToString(),
    };
}

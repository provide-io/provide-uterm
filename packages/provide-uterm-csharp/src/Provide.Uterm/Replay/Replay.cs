//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;

namespace Provide.Uterm.Replay;

/// <summary>
/// Rebuild and replay terminal sessions from JSONL logs.
/// Port of provide.uterm.replay / packages/provide-uterm-go/replay.
/// </summary>
public static class Replay
{
    public static async Task RebuildRawStreamAsync(string logPath, string outPath)
    {
        var outBytes = new List<byte>();
        await foreach (var line in File.ReadLinesAsync(logPath))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            using var doc = JsonDocument.Parse(line);
            var root = doc.RootElement;
            if (root.TryGetProperty("event", out var ev) && ev.GetString() != "read")
            {
                continue;
            }

            if (!root.TryGetProperty("data", out var data) ||
                !data.TryGetProperty("raw_bytes_b64", out var b64El))
            {
                continue;
            }

            var b64 = b64El.GetString();
            if (string.IsNullOrEmpty(b64))
            {
                continue;
            }

            outBytes.AddRange(Convert.FromBase64String(b64));
        }

        await File.WriteAllBytesAsync(outPath, outBytes.ToArray());
        try
        {
            File.SetUnixFileMode(outPath, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
        catch (PlatformNotSupportedException)
        {
        }
    }

    public sealed class ReplayOptions
    {
        public double Speed { get; set; } = 1.0;
        public bool Step { get; set; }
        public IReadOnlyList<string>? Events { get; set; }
        public TextWriter? Output { get; set; }
        public TextReader? Input { get; set; }
        public Action<TimeSpan>? Sleep { get; set; }
    }

    public static async Task ReplayLogAsync(string logPath, ReplayOptions? opts = null)
    {
        opts ??= new ReplayOptions();
        if (opts.Speed == 0)
        {
            opts.Speed = 1.0;
        }

        opts.Output ??= Console.Out;
        opts.Input ??= Console.In;
        opts.Sleep ??= t => Thread.Sleep(t);
        var events = opts.Events is { Count: > 0 } ? opts.Events : new[] { "read", "screen" };
        var wanted = events.ToHashSet(StringComparer.Ordinal);

        double lastTs = 0;
        var haveTs = false;
        await foreach (var line in File.ReadLinesAsync(logPath))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            JsonDocument doc;
            try
            {
                doc = JsonDocument.Parse(line);
            }
            catch
            {
                continue;
            }

            using (doc)
            {
                var root = doc.RootElement;
                if (!root.TryGetProperty("event", out var evEl) ||
                    !wanted.Contains(evEl.GetString() ?? ""))
                {
                    continue;
                }

                if (!root.TryGetProperty("data", out var data) ||
                    !data.TryGetProperty("screen", out var screenEl) ||
                    screenEl.ValueKind == JsonValueKind.Null)
                {
                    continue;
                }

                var screen = screenEl.GetString() ?? "";
                if (haveTs && !opts.Step && root.TryGetProperty("ts", out var tsEl) &&
                    tsEl.TryGetDouble(out var ts))
                {
                    var speed = Math.Min(Math.Max(opts.Speed, 0.01), 100.0);
                    var delta = (ts - lastTs) / speed;
                    if (delta > 0)
                    {
                        opts.Sleep(TimeSpan.FromSeconds(delta));
                    }
                }

                if (root.TryGetProperty("ts", out var tsEl2) && tsEl2.TryGetDouble(out var ts2))
                {
                    lastTs = ts2;
                    haveTs = true;
                }

                await opts.Output.WriteAsync("\x1b[2J\x1b[H" + screen);
                if (opts.Step)
                {
                    await opts.Output.WriteAsync("-- next --");
                    await opts.Input.ReadLineAsync();
                }
            }
        }
    }
}

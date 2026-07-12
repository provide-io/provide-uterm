//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Channels;

/// <summary>Client-advertised typed-channel versions.</summary>
public sealed class Hello
{
    public Dictionary<string, int> Channels { get; init; } = new();
}

/// <summary>
/// Generic typed-channel negotiation over the inline control channel.
/// Port of provide.uterm.channels / packages/provide-uterm-go/channels.
/// </summary>
public sealed class Negotiated
{
    private readonly Dictionary<string, int> _supported;
    private readonly string _defaultChannel;
    private readonly bool _hasDefault;
    private Dictionary<string, int> _granted = new();
    private readonly Dictionary<string, int> _seq = new();

    private Negotiated(Dictionary<string, int> supported, string defaultChannel, bool hasDefault)
    {
        _supported = supported;
        _defaultChannel = defaultChannel;
        _hasDefault = hasDefault;
    }

    /// <summary>
    /// Build a Negotiated set from the supported channel→version map.
    /// </summary>
    public static Negotiated Create(IReadOnlyDictionary<string, int> supported, string defaultChannel = "")
    {
        var normalized = NormalizeSupported(supported);
        if (defaultChannel.Length > 0 && !normalized.ContainsKey(defaultChannel))
        {
            throw new ArgumentException($"default channel is not supported: \"{defaultChannel}\"");
        }

        return new Negotiated(normalized, defaultChannel, defaultChannel.Length > 0);
    }

    public IReadOnlyDictionary<string, int> Granted()
    {
        return new Dictionary<string, int>(_granted);
    }

    public bool IsNegotiated(string channel = "")
    {
        var selected = SelectChannel(channel);
        return _granted.ContainsKey(selected);
    }

    public Dictionary<string, object?> HandleHello(Hello hello, IReadOnlyDictionary<string, object?>? ackFields = null)
    {
        ackFields ??= new Dictionary<string, object?>();
        foreach (var key in ackFields.Keys.OrderBy(k => k, StringComparer.Ordinal))
        {
            if (key is "type" or "channels")
            {
                throw new ArgumentException($"reserved hello_ack field: {key}");
            }
        }

        _granted = Negotiate(_supported, hello.Channels);
        var ack = new Dictionary<string, object?>
        {
            ["type"] = "hello_ack",
            ["channels"] = new Dictionary<string, int>(_granted),
        };
        foreach (var (k, v) in ackFields)
        {
            ack[k] = v;
        }

        return ack;
    }

    public int NextSeq(string channel = "")
    {
        var selected = SelectChannel(channel);
        _seq.TryGetValue(selected, out var n);
        n++;
        _seq[selected] = n;
        return n;
    }

    public IReadOnlyDictionary<string, int> ExportGrants() => Granted();

    public void RestoreGrants(IReadOnlyDictionary<string, object?> grants)
    {
        var coerced = CoerceChannelMap(grants);
        _granted = Negotiate(_supported, coerced);
        _seq.Clear();
    }

    private string SelectChannel(string channel)
    {
        if (channel.Length > 0)
        {
            return channel;
        }

        if (!_hasDefault)
        {
            throw new InvalidOperationException("channel is required when no default_channel is configured");
        }

        return _defaultChannel;
    }

    /// <summary>
    /// Parse a framed hello payload, returning null when raw is not a channel hello.
    /// </summary>
    public static Hello? ParseChannelHello(string raw)
    {
        if (string.IsNullOrEmpty(raw) || !ControlChannelCodec.IsControlFrame(raw))
        {
            return null;
        }

        foreach (var chunk in DecodeFrames(raw))
        {
            if (chunk is not ControlChunk ctrl)
            {
                continue;
            }

            if (!ctrl.Control.TryGetValue("type", out var typeObj) || typeObj as string != "hello")
            {
                continue;
            }

            if (!ctrl.Control.TryGetValue("channels", out var channelsRaw) || channelsRaw is not IDictionary<string, object?> map)
            {
                // also accept Dictionary
                if (channelsRaw is Dictionary<string, object?> d)
                {
                    map = d;
                }
                else
                {
                    return null;
                }
            }

            try
            {
                var coerced = CoerceChannelMap(map);
                return new Hello { Channels = coerced };
            }
            catch
            {
                return null;
            }
        }

        return null;
    }

    private static IReadOnlyList<Chunk> DecodeFrames(string raw)
    {
        try
        {
            var decoder = new ControlFrameDecoder(new DecoderOptions());
            var chunks = decoder.Feed(raw).ToList();
            chunks.AddRange(decoder.Finish());
            return chunks;
        }
        catch
        {
            return Array.Empty<Chunk>();
        }
    }

    private static Dictionary<string, int> NormalizeSupported(IReadOnlyDictionary<string, int> supported)
    {
        var normalized = new Dictionary<string, int>();
        foreach (var (name, version) in supported)
        {
            if (string.IsNullOrEmpty(name))
            {
                throw new ArgumentException("channel names must be non-empty strings");
            }

            normalized[name] = version;
        }

        if (normalized.Count == 0)
        {
            throw new ArgumentException("at least one supported channel is required");
        }

        return normalized;
    }

    internal static Dictionary<string, int> CoerceChannelMap(IEnumerable<KeyValuePair<string, object?>> value)
    {
        var channels = new Dictionary<string, int>();
        foreach (var (name, version) in value)
        {
            if (string.IsNullOrEmpty(name))
            {
                throw new ArgumentException("channel names must be non-empty strings");
            }

            channels[name] = CoerceInt(version);
        }

        return channels;
    }

    private static int CoerceInt(object? version) =>
        version switch
        {
            int i => i,
            long l => (int)l,
            double d when d == Math.Truncate(d) && !double.IsInfinity(d) => (int)d,
            float f when f == Math.Truncate(f) && !float.IsInfinity(f) => (int)f,
            JsonElement je when je.ValueKind == JsonValueKind.Number && je.TryGetInt64(out var n) => (int)n,
            _ => throw new ArgumentException("channel versions must be integers"),
        };

    private static Dictionary<string, int> Negotiate(
        IReadOnlyDictionary<string, int> supported,
        IReadOnlyDictionary<string, int> requested)
    {
        var granted = new Dictionary<string, int>();
        foreach (var (name, version) in requested)
        {
            if (supported.TryGetValue(name, out var supportedVersion) && version > 0)
            {
                granted[name] = Math.Min(version, supportedVersion);
            }
        }

        return granted;
    }
}

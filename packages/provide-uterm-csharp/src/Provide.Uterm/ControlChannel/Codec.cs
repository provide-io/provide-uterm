//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.ControlChannel;

/// <summary>
/// Inline DLE/STX control framing used to mix terminal data and JSON control
/// messages in one WebSocket stream. Port of provide.uterm.control_channel /
/// packages/provide-uterm-go/controlchannel.
///
/// Internally the decoder buffers UTF-8 bytes (matching Go string semantics).
/// Public APIs accept/return .NET strings via UTF-8 round-trip.
/// </summary>
public static class ControlChannelCodec
{
    public const string Dle = "\u0010";
    public const string Stx = "\u0002";
    public const byte DleByte = 0x10;
    public const byte StxByte = 0x02;

    internal const int HeaderBytes = 11; // DLE STX + 8 hex digits + ':'
    internal const int MaxControlPayloadBytes = 1_048_576;
    internal const int DefaultMaxBufferBytes = 10_485_760;
    internal const int DefaultMaxFrameDepth = 32;

    /// <summary>Encode terminal data by escaping every DLE byte.</summary>
    public static string EncodeTerminalData(string data) =>
        data.Replace(Dle, Dle + Dle, StringComparison.Ordinal);

    /// <summary>
    /// Encode a control payload. JSON is compact (no spaces) and does not
    /// escape HTML metacharacters, matching the Python encoder.
    /// </summary>
    public static string EncodeControlFrame(IReadOnlyDictionary<string, object?> payload)
    {
        var serialized = MarshalCompact(payload);
        var utf8Len = Encoding.UTF8.GetByteCount(serialized);
        return $"{Dle}{Stx}{utf8Len:x8}:{serialized}";
    }

    public static string EncodeControlFrame(Dictionary<string, object?> payload) =>
        EncodeControlFrame((IReadOnlyDictionary<string, object?>)payload);

    internal static string MarshalCompact(IReadOnlyDictionary<string, object?> payload)
    {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions
        {
            Indented = false,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }))
        {
            WriteValue(writer, payload);
        }

        return Encoding.UTF8.GetString(stream.ToArray());
    }

    private static void WriteValue(Utf8JsonWriter writer, object? value)
    {
        switch (value)
        {
            case null:
                writer.WriteNullValue();
                break;
            case bool b:
                writer.WriteBooleanValue(b);
                break;
            case string s:
                writer.WriteStringValue(s);
                break;
            case byte or sbyte or short or ushort or int or uint or long or ulong:
                writer.WriteNumberValue(Convert.ToInt64(value, CultureInfo.InvariantCulture));
                break;
            case float f:
                writer.WriteNumberValue(f);
                break;
            case double d:
                writer.WriteNumberValue(d);
                break;
            case decimal m:
                writer.WriteNumberValue(m);
                break;
            case JsonElement je:
                je.WriteTo(writer);
                break;
            case IReadOnlyDictionary<string, object?> dict:
                writer.WriteStartObject();
                foreach (var (k, v) in dict)
                {
                    writer.WritePropertyName(k);
                    WriteValue(writer, v);
                }

                writer.WriteEndObject();
                break;
            case IDictionary<string, object?> dict:
                writer.WriteStartObject();
                foreach (var (k, v) in dict)
                {
                    writer.WritePropertyName(k);
                    WriteValue(writer, v);
                }

                writer.WriteEndObject();
                break;
            case System.Collections.IEnumerable list when value is not string:
                writer.WriteStartArray();
                foreach (var item in list)
                {
                    WriteValue(writer, item);
                }

                writer.WriteEndArray();
                break;
            default:
                writer.WriteStringValue(Convert.ToString(value, CultureInfo.InvariantCulture));
                break;
        }
    }

    public static Dictionary<string, object?> JsonElementToDictionary(JsonElement el)
    {
        var dict = new Dictionary<string, object?>();
        foreach (var prop in el.EnumerateObject())
        {
            dict[prop.Name] = JsonElementToObject(prop.Value);
        }

        return dict;
    }

    /// <summary>
    /// Convert JSON to CLR objects, preserving integer vs float distinction
    /// (integers stay long; fractional values stay double) for HMAC canonicalization.
    /// </summary>
    internal static object? JsonElementToObject(JsonElement el) =>
        el.ValueKind switch
        {
            JsonValueKind.Object => JsonElementToDictionary(el),
            JsonValueKind.Array => el.EnumerateArray().Select(JsonElementToObject).Cast<object?>().ToList(),
            JsonValueKind.String => el.GetString(),
            JsonValueKind.Number => NumberFromJson(el),
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.Null => null,
            _ => el.GetRawText(),
        };

    private static object NumberFromJson(JsonElement el)
    {
        var raw = el.GetRawText();
        if (raw.IndexOfAny(['.', 'e', 'E']) < 0 && el.TryGetInt64(out var l))
        {
            return l;
        }

        return el.GetDouble();
    }

    /// <summary>
    /// Structural check: magic, length header syntax, and full payload present.
    /// </summary>
    public static bool IsControlFrame(string message)
    {
        var bytes = Encoding.UTF8.GetBytes(message);
        return IsControlFrame(bytes);
    }

    /// <summary>Structural control-frame check over the original wire bytes.</summary>
    public static bool IsControlFrame(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length < HeaderBytes)
        {
            return false;
        }

        if (bytes[0] != DleByte || bytes[1] != StxByte)
        {
            return false;
        }

        if (bytes[10] != (byte)':')
        {
            return false;
        }

        var lengthHex = Encoding.ASCII.GetString(bytes.Slice(2, 8));
        if (!TryParseHex32(lengthHex, out var payloadBytes))
        {
            return false;
        }

        if (string.Create(CultureInfo.InvariantCulture, $"{payloadBytes:x8}") != lengthHex)
        {
            return false;
        }

        if (payloadBytes > MaxControlPayloadBytes)
        {
            return false;
        }

        try
        {
            // Narrowed only after the size guard above, for the reason in
            // TryParseHex32: the header is unsigned and this argument is not.
            var payloadEnd = Utf8PayloadEnd(bytes, HeaderBytes, (int)payloadBytes);
            return payloadEnd == bytes.Length;
        }
        catch (ProtocolException)
        {
            return false;
        }
    }

    /// <summary>
    /// Parse the frame's eight-digit hex length header.
    /// </summary>
    /// <remarks>
    /// The accumulator is <see cref="long"/>, not <see cref="int"/>, and that is
    /// the whole point: the header is an <em>unsigned</em> 32-bit wire value, so
    /// <c>80000000</c> through <c>ffffffff</c> overflow a signed 32-bit
    /// accumulator and wrap negative. A negative length then slips past every
    /// <c>&gt; MaxControlPayloadBytes</c> guard — which only ever looks upward —
    /// and reaches an index or a slice, where it throws a raw
    /// <see cref="IndexOutOfRangeException"/> instead of the protocol error a
    /// caller is catching. Thirteen bytes from a peer were enough. The reference
    /// parses the same header with arbitrary-precision arithmetic and reports
    /// "control payload too large"; pinned as <c>CCF-REG-0005</c> in the
    /// cross-language fuzz corpus, which is what found this.
    ///
    /// Callers must therefore keep the value wide until after the size guard,
    /// and only then narrow it.
    /// </remarks>
    internal static bool TryParseHex32(ReadOnlySpan<char> s, out long value)
    {
        value = 0;
        if (s.Length != 8)
        {
            return false;
        }

        for (var i = 0; i < s.Length; i++)
        {
            var c = s[i];
            int d;
            if (c is >= '0' and <= '9')
            {
                d = c - '0';
            }
            else if (c is >= 'a' and <= 'f')
            {
                d = c - 'a' + 10;
            }
            else if (c is >= 'A' and <= 'F')
            {
                d = c - 'A' + 10;
            }
            else
            {
                return false;
            }

            value = (value << 4) | d;
        }

        return true;
    }

    /// <summary>
    /// Byte index ending a payload of <paramref name="payloadBytes"/> UTF-8
    /// bytes starting at <paramref name="start"/>, or -1 when incomplete.
    /// Throws when the declared length splits a multi-byte rune.
    /// </summary>
    internal static int Utf8PayloadEnd(ReadOnlySpan<byte> buf, int start, int payloadBytes)
    {
        var end = start + payloadBytes;
        if (end > buf.Length)
        {
            return -1;
        }

        if (end < buf.Length && IsUtf8Continuation(buf[end]))
        {
            throw new ProtocolException("invalid control payload length");
        }

        return end;
    }

    private static bool IsUtf8Continuation(byte b) => (b & 0xC0) == 0x80;
}

/// <summary>Raised when an inline control frame is malformed.</summary>
public sealed class ProtocolException : Exception
{
    public ProtocolException(string message)
        : base(message)
    {
    }
}

/// <summary>Decoded element of the inline stream.</summary>
public abstract class Chunk
{
    public abstract string Kind { get; }
}

/// <summary>Decoded terminal data (UTF-8 decoded string, latin-1 safe for BBS).</summary>
public sealed class DataChunk : Chunk
{
    public DataChunk(string data) => Data = data;

    public string Data { get; }

    public override string Kind => "data";
}

/// <summary>Decoded control payload.</summary>
public sealed class ControlChunk : Chunk
{
    public ControlChunk(Dictionary<string, object?> control) => Control = control;

    public Dictionary<string, object?> Control { get; }

    /// <summary>Alias for <see cref="Control"/> (some call sites use Payload).</summary>
    public Dictionary<string, object?> Payload => Control;

    public override string Kind => "control";
}

/// <summary>Decoder options. Zero/defaults select protocol defaults.</summary>
public sealed class DecoderOptions
{
    public int MaxControlPayloadBytes { get; set; }
    public int MaxBufferBytes { get; set; }
    public int MaxFrameDepth { get; set; }
    public Action<string>? OnError { get; set; }
}

/// <summary>Incrementally decodes the inline DLE/STX control-frame stream.</summary>
public class ControlFrameDecoder
{
    private readonly int _maxControlPayloadBytes;
    private readonly int _maxBufferBytes;
    private readonly int _maxFrameDepth;
    private readonly Action<string>? _onError;
    private byte[] _buffered = Array.Empty<byte>();
    private int _bufferedLen;

    public ControlFrameDecoder(DecoderOptions? options = null)
    {
        options ??= new DecoderOptions();
        _maxControlPayloadBytes = options.MaxControlPayloadBytes < 1
            ? ControlChannelCodec.MaxControlPayloadBytes
            : options.MaxControlPayloadBytes;
        _maxBufferBytes = options.MaxBufferBytes < 1
            ? ControlChannelCodec.DefaultMaxBufferBytes
            : options.MaxBufferBytes;
        _maxFrameDepth = options.MaxFrameDepth < 1
            ? ControlChannelCodec.DefaultMaxFrameDepth
            : options.MaxFrameDepth;
        _onError = options.OnError;
    }

    private Exception ReportError(string message)
    {
        _onError?.Invoke("control_frame_protocol_error");
        return new ProtocolException(message);
    }

    private void Reset()
    {
        _buffered = Array.Empty<byte>();
        _bufferedLen = 0;
    }

    private void Append(ReadOnlySpan<byte> chunk)
    {
        if (_bufferedLen + chunk.Length > _buffered.Length)
        {
            var next = Math.Max(_buffered.Length == 0 ? 256 : _buffered.Length * 2, _bufferedLen + chunk.Length);
            Array.Resize(ref _buffered, next);
        }

        chunk.CopyTo(_buffered.AsSpan(_bufferedLen));
        _bufferedLen += chunk.Length;
    }

    /// <summary>Decode all complete events from <paramref name="chunk"/>.</summary>
    public IReadOnlyList<Chunk> Feed(string chunk)
    {
        var bytes = Encoding.UTF8.GetBytes(chunk);
        return FeedBytes(bytes);
    }

    /// <summary>
    /// Decode directly from wire bytes. Binary terminal data can request a
    /// one-byte-to-one-character mapping while control JSON remains UTF-8.
    /// </summary>
    public IReadOnlyList<Chunk> FeedBytes(ReadOnlySpan<byte> chunk, bool preserveRawData = false)
    {
        if (_bufferedLen + chunk.Length > _maxBufferBytes)
        {
            var total = _bufferedLen + chunk.Length;
            Reset();
            throw ReportError($"control frame buffer overflow: {total} > {_maxBufferBytes}");
        }

        Append(chunk);
        try
        {
            return Drain(final: false, preserveRawData: preserveRawData);
        }
        catch
        {
            Reset();
            throw;
        }
    }

    /// <summary>Decode remaining buffered data; reject truncated frames.</summary>
    public IReadOnlyList<Chunk> Finish()
    {
        try
        {
            return Drain(final: true, preserveRawData: false);
        }
        catch
        {
            Reset();
            throw;
        }
    }

    private Dictionary<string, object?> ParseFramePayload(ReadOnlySpan<byte> payloadRaw)
    {
        JsonDocument doc;
        try
        {
            doc = JsonDocument.Parse(payloadRaw.ToArray());
        }
        catch (JsonException)
        {
            throw ReportError("invalid control json");
        }

        using (doc)
        {
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw ReportError("control payload must be an object");
            }

            var obj = ControlChannelCodec.JsonElementToDictionary(doc.RootElement);
            CheckJsonDepth(obj, _maxFrameDepth);
            return obj;
        }
    }

    private void CheckJsonDepth(object? value, int maxDepth)
    {
        var stack = new Stack<(object? Node, int Depth)>();
        stack.Push((value, 1));
        while (stack.Count > 0)
        {
            var (node, depth) = stack.Pop();
            if (depth > maxDepth)
            {
                throw ReportError($"control payload nests deeper than {maxDepth}");
            }

            switch (node)
            {
                case Dictionary<string, object?> map:
                    foreach (var child in map.Values)
                    {
                        if (child is Dictionary<string, object?> or System.Collections.IList)
                        {
                            stack.Push((child, depth + 1));
                        }
                    }

                    break;
                case System.Collections.IList list:
                    foreach (var child in list)
                    {
                        if (child is Dictionary<string, object?> or System.Collections.IList)
                        {
                            stack.Push((child, depth + 1));
                        }
                    }

                    break;
            }
        }
    }

    private (ControlChunk? Chunk, int FrameEnd, bool Done) TryParseFrame(ReadOnlySpan<byte> buf, int idx, bool final)
    {
        if (buf.Length - idx < ControlChannelCodec.HeaderBytes)
        {
            if (final)
            {
                throw ReportError("truncated control frame");
            }

            return (null, 0, false);
        }

        var lengthHex = Encoding.ASCII.GetString(buf.Slice(idx + 2, 8));
        var separator = buf[idx + 10];
        if (separator != (byte)':' || !ControlChannelCodec.TryParseHex32(lengthHex, out var payloadBytes))
        {
            throw ReportError("invalid control header");
        }

        if (payloadBytes > ControlChannelCodec.MaxControlPayloadBytes || payloadBytes > _maxControlPayloadBytes)
        {
            throw ReportError("control payload too large");
        }

        var payloadStart = idx + ControlChannelCodec.HeaderBytes;
        // Narrowed only here, after both size guards above have run. The header
        // is an unsigned 32-bit value; narrowing before the guard is what let a
        // negative length reach this index. See TryParseHex32.
        var payloadLength = (int)payloadBytes;
        int end;
        try
        {
            end = ControlChannelCodec.Utf8PayloadEnd(buf, payloadStart, payloadLength);
        }
        catch (ProtocolException ex)
        {
            throw ReportError(ex.Message);
        }

        if (end == -1)
        {
            if (final)
            {
                throw ReportError("truncated control frame");
            }

            return (null, 0, false);
        }

        var payload = ParseFramePayload(buf.Slice(payloadStart, payloadLength));
        return (new ControlChunk(payload), end, true);
    }

    private List<Chunk> Drain(bool final, bool preserveRawData)
    {
        var events = new List<Chunk>();
        // Copy to array so local functions can capture without ref-struct issues.
        var buf = new byte[_bufferedLen];
        Buffer.BlockCopy(_buffered, 0, buf, 0, _bufferedLen);
        var bufLen = buf.Length;
        var idx = 0;
        var dataParts = new List<byte>();
        var dataStart = 0;

        void EmitData(int upTo)
        {
            if (dataStart < upTo)
            {
                for (var i = dataStart; i < upTo; i++)
                {
                    dataParts.Add(buf[i]);
                }
            }

            if (dataParts.Count > 0)
            {
                var data = dataParts.ToArray();
                events.Add(new DataChunk(preserveRawData
                    ? Encoding.Latin1.GetString(data)
                    : Encoding.UTF8.GetString(data)));
                dataParts.Clear();
            }
        }

        while (idx < bufLen)
        {
            if (buf[idx] != ControlChannelCodec.DleByte)
            {
                idx++;
                continue;
            }

            if (idx + 1 >= bufLen)
            {
                if (final)
                {
                    throw ReportError("truncated control frame");
                }

                break;
            }

            var next = buf[idx + 1];
            if (next == ControlChannelCodec.DleByte)
            {
                if (dataStart < idx)
                {
                    for (var i = dataStart; i < idx; i++)
                    {
                        dataParts.Add(buf[i]);
                    }
                }

                dataParts.Add(ControlChannelCodec.DleByte);
                idx += 2;
                dataStart = idx;
                continue;
            }

            if (next != ControlChannelCodec.StxByte)
            {
                throw ReportError("invalid control prefix");
            }

            EmitData(idx);
            dataStart = idx;

            var (chunk, frameEnd, done) = TryParseFrame(buf, idx, final);
            if (!done)
            {
                break;
            }

            idx = frameEnd;
            dataStart = idx;
            if (chunk is not null)
            {
                events.Add(chunk);
            }
        }

        if (idx > 0)
        {
            var remaining = _bufferedLen - idx;
            if (remaining > 0)
            {
                Buffer.BlockCopy(_buffered, idx, _buffered, 0, remaining);
            }

            _bufferedLen = remaining;
        }

        EmitData(idx);
        return events;
    }
}

/// <summary>Go/Python-aligned name for <see cref="ControlFrameDecoder"/>.</summary>
public sealed class Decoder : ControlFrameDecoder
{
    public Decoder(DecoderOptions? options = null) : base(options) { }
}

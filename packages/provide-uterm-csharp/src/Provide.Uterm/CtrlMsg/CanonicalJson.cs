//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.CtrlMsg;

/// <summary>
/// Serialises values exactly as CPython's
/// json.dumps(v, sort_keys=True, separators=(",", ":")) with ensure_ascii=True.
/// Load-bearing for identity-frame HMAC signatures.
/// </summary>
public static class CanonicalJson
{
    public static string Serialize(object? v)
    {
        var b = new StringBuilder();
        Encode(b, v);
        return b.ToString();
    }

    private static void Encode(StringBuilder b, object? v)
    {
        switch (v)
        {
            case null:
                b.Append("null");
                break;
            case bool x:
                b.Append(x ? "true" : "false");
                break;
            case string s:
                EncodeString(b, s);
                break;
            case byte or sbyte or short or ushort or int or uint:
                b.Append(Convert.ToInt64(v, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture));
                break;
            case long l:
                b.Append(l.ToString(CultureInfo.InvariantCulture));
                break;
            case ulong ul:
                b.Append(ul.ToString(CultureInfo.InvariantCulture));
                break;
            case float f:
                b.Append(PyFloatRepr(f));
                break;
            case double d:
                b.Append(PyFloatRepr(d));
                break;
            case decimal m:
                b.Append(PyFloatRepr((double)m));
                break;
            case JsonElement je:
                EncodeJsonElement(b, je);
                break;
            case IReadOnlyDictionary<string, object?> map:
                EncodeMap(b, map);
                break;
            case IDictionary<string, object?> map:
                EncodeMap(b, map);
                break;
            case System.Collections.IDictionary dict:
            {
                var converted = new Dictionary<string, object?>();
                foreach (System.Collections.DictionaryEntry e in dict)
                {
                    converted[Convert.ToString(e.Key, CultureInfo.InvariantCulture)!] = e.Value;
                }

                EncodeMap(b, converted);
                break;
            }
            case System.Collections.IEnumerable list when v is not string:
                EncodeList(b, list);
                break;
            default:
                throw new ArgumentException($"ctrlmsg: cannot canonically encode value of type {v.GetType()}");
        }
    }

    private static void EncodeJsonElement(StringBuilder b, JsonElement je)
    {
        switch (je.ValueKind)
        {
            case JsonValueKind.Null:
                b.Append("null");
                break;
            case JsonValueKind.True:
                b.Append("true");
                break;
            case JsonValueKind.False:
                b.Append("false");
                break;
            case JsonValueKind.String:
                EncodeString(b, je.GetString() ?? "");
                break;
            case JsonValueKind.Number:
            {
                var raw = je.GetRawText();
                if (raw.IndexOfAny(['.', 'e', 'E']) >= 0)
                {
                    b.Append(PyFloatRepr(je.GetDouble()));
                }
                else
                {
                    b.Append(raw);
                }

                break;
            }
            case JsonValueKind.Object:
            {
                var keys = je.EnumerateObject().Select(p => p.Name).OrderBy(k => k, StringComparer.Ordinal).ToList();
                b.Append('{');
                for (var i = 0; i < keys.Count; i++)
                {
                    if (i > 0)
                    {
                        b.Append(',');
                    }

                    EncodeString(b, keys[i]);
                    b.Append(':');
                    EncodeJsonElement(b, je.GetProperty(keys[i]));
                }

                b.Append('}');
                break;
            }
            case JsonValueKind.Array:
            {
                b.Append('[');
                var first = true;
                foreach (var item in je.EnumerateArray())
                {
                    if (!first)
                    {
                        b.Append(',');
                    }

                    first = false;
                    EncodeJsonElement(b, item);
                }

                b.Append(']');
                break;
            }
            default:
                throw new ArgumentException($"ctrlmsg: cannot canonically encode JsonElement {je.ValueKind}");
        }
    }

    private static void EncodeMap(StringBuilder b, IEnumerable<KeyValuePair<string, object?>> map)
    {
        var pairs = map.OrderBy(kv => kv.Key, StringComparer.Ordinal).ToList();
        b.Append('{');
        for (var i = 0; i < pairs.Count; i++)
        {
            if (i > 0)
            {
                b.Append(',');
            }

            EncodeString(b, pairs[i].Key);
            b.Append(':');
            Encode(b, pairs[i].Value);
        }

        b.Append('}');
    }

    private static void EncodeList(StringBuilder b, System.Collections.IEnumerable list)
    {
        b.Append('[');
        var first = true;
        foreach (var e in list)
        {
            if (!first)
            {
                b.Append(',');
            }

            first = false;
            Encode(b, e);
        }

        b.Append(']');
    }

    private static void EncodeString(StringBuilder b, string s)
    {
        b.Append('"');
        var i = 0;
        while (i < s.Length)
        {
            var r = char.ConvertToUtf32(s, i);
            i += char.IsSurrogatePair(s, i) ? 2 : 1;
            switch (r)
            {
                case '"':
                    b.Append("\\\"");
                    break;
                case '\\':
                    b.Append("\\\\");
                    break;
                case '\n':
                    b.Append("\\n");
                    break;
                case '\r':
                    b.Append("\\r");
                    break;
                case '\t':
                    b.Append("\\t");
                    break;
                case '\b':
                    b.Append("\\b");
                    break;
                case '\f':
                    b.Append("\\f");
                    break;
                default:
                    if (r is >= 0x20 and <= 0x7e)
                    {
                        b.Append((char)r);
                    }
                    else if (r > 0xffff)
                    {
                        var v = r - 0x10000;
                        var hi = 0xd800 + (v >> 10);
                        var lo = 0xdc00 + (v & 0x3ff);
                        b.Append(CultureInfo.InvariantCulture, $"\\u{hi:x4}\\u{lo:x4}");
                    }
                    else
                    {
                        b.Append(CultureInfo.InvariantCulture, $"\\u{r:x4}");
                    }

                    break;
            }
        }

        b.Append('"');
    }

    /// <summary>
    /// Format f exactly as CPython's repr()/json.dumps would: shortest
    /// round-trip decimal with Python fixed/exponential thresholds.
    /// Mirrors packages/provide-uterm-go/ctrlmsg pyFloatRepr.
    /// </summary>
    public static string PyFloatRepr(double f)
    {
        if (double.IsNaN(f))
        {
            return "NaN";
        }

        if (double.IsPositiveInfinity(f))
        {
            return "Infinity";
        }

        if (double.IsNegativeInfinity(f))
        {
            return "-Infinity";
        }

        // Shortest round-trip scientific form (Go: strconv.FormatFloat(f, 'e', -1, 64)).
        var sci = FormatFloatScientificShortest(f);
        var neg = false;
        if (sci[0] == '-')
        {
            neg = true;
            sci = sci[1..];
        }

        var ePos = sci.IndexOf('e');
        var mantissa = sci[..ePos];
        var exp = int.Parse(sci[(ePos + 1)..], CultureInfo.InvariantCulture);
        var digits = mantissa.Replace(".", "", StringComparison.Ordinal);
        var ndigits = digits.Length;
        var decpt = exp + 1;

        string body;
        if (decpt <= -4 || decpt > 16)
        {
            body = FormatExponential(digits, decpt);
        }
        else
        {
            body = FormatFixed(digits, ndigits, decpt);
        }

        return neg ? "-" + body : body;
    }

    /// <summary>
    /// Shortest round-trip scientific form like Go's FormatFloat(f,'e',-1,64):
    /// e.g. "1.5e+00", "1e+16", "0e+00", "-0e+00".
    /// </summary>
    private static string FormatFloatScientificShortest(double f)
    {
        if (f == 0.0)
        {
            return double.IsNegative(f) ? "-0e+00" : "0e+00";
        }

        // "R" is round-trip; convert to scientific digits + exponent.
        var g = f.ToString("R", CultureInfo.InvariantCulture);
        // Handle if R already used E notation
        if (g.Contains('E', StringComparison.OrdinalIgnoreCase))
        {
            return NormalizeScientificFromR(g);
        }

        var neg = g.StartsWith('-');
        if (neg)
        {
            g = g[1..];
        }

        string digits;
        int exp;
        if (g.Contains('.'))
        {
            var dot = g.IndexOf('.');
            var intPart = g[..dot];
            var frac = g[(dot + 1)..];
            if (intPart == "0" || intPart.Length == 0)
            {
                // 0.00xyz
                var trimmed = frac.TrimStart('0');
                var lead = frac.Length - trimmed.Length;
                digits = trimmed.Length == 0 ? "0" : trimmed;
                exp = -lead - 1;
            }
            else
            {
                digits = (intPart + frac).TrimEnd('0');
                exp = intPart.Length - 1;
            }
        }
        else
        {
            digits = g.TrimStart('0');
            if (digits.Length == 0)
            {
                digits = "0";
            }

            exp = digits.Length - 1;
        }

        // Drop leading zeros that could appear after trim anomalies
        while (digits.Length > 1 && digits[0] == '0')
        {
            digits = digits[1..];
            exp--;
        }

        // Shortest mantissa: drop trailing zeros (1e+16 is "1", not "1000...0").
        digits = digits.TrimEnd('0');
        if (digits.Length == 0)
        {
            digits = "0";
        }

        var mant = digits.Length == 1 ? digits : digits[0] + "." + digits[1..];
        var expStr = exp >= 0
            ? "+" + exp.ToString(CultureInfo.InvariantCulture).PadLeft(2, '0')
            : "-" + (-exp).ToString(CultureInfo.InvariantCulture).PadLeft(2, '0');
        var result = (neg ? "-" : "") + mant + "e" + expStr;

        // Verify round-trip; if R→sci failed, fall back to high-precision e-format trim.
        if (double.TryParse(result, NumberStyles.Float, CultureInfo.InvariantCulture, out var back) &&
            BitConverter.DoubleToInt64Bits(back) == BitConverter.DoubleToInt64Bits(f))
        {
            return result;
        }

        return FallbackScientific(f);
    }

    private static string NormalizeScientificFromR(string g)
    {
        // e.g. -1.5E+16 or 1E-05
        var neg = g.StartsWith('-');
        if (neg)
        {
            g = g[1..];
        }

        var parts = g.Split('E', 'e');
        var mant = parts[0];
        var exp = int.Parse(parts[1], CultureInfo.InvariantCulture);
        if (mant.Contains('.'))
        {
            mant = mant.TrimEnd('0').TrimEnd('.');
        }

        var expStr = exp >= 0
            ? "+" + exp.ToString(CultureInfo.InvariantCulture).PadLeft(2, '0')
            : "-" + (-exp).ToString(CultureInfo.InvariantCulture).PadLeft(2, '0');
        return (neg ? "-" : "") + mant + "e" + expStr;
    }

    private static string FallbackScientific(double f)
    {
        // Use enough digits then trim trailing zeros in mantissa.
        var s = f.ToString("e17", CultureInfo.InvariantCulture);
        // 1.00000000000000000e+000
        var ePos = s.IndexOf('e');
        var mant = s[..ePos];
        var exp = int.Parse(s[(ePos + 1)..], CultureInfo.InvariantCulture);
        if (mant.Contains('.'))
        {
            mant = mant.TrimEnd('0').TrimEnd('.');
        }

        var expStr = exp >= 0
            ? "+" + Math.Abs(exp).ToString(CultureInfo.InvariantCulture).PadLeft(2, '0')
            : "-" + Math.Abs(exp).ToString(CultureInfo.InvariantCulture).PadLeft(2, '0');
        return mant + "e" + expStr;
    }

    private static string FormatFixed(string digits, int ndigits, int decpt)
    {
        if (decpt <= 0)
        {
            return "0." + new string('0', -decpt) + digits;
        }

        if (decpt >= ndigits)
        {
            return digits + new string('0', decpt - ndigits) + ".0";
        }

        return digits[..decpt] + "." + digits[decpt..];
    }

    private static string FormatExponential(string digits, int decpt)
    {
        var mant = digits.Length == 1 ? digits : digits[0] + "." + digits[1..];
        var exp = decpt - 1;
        var sign = exp < 0 ? "-" : "+";
        if (exp < 0)
        {
            exp = -exp;
        }

        var es = exp.ToString(CultureInfo.InvariantCulture);
        if (es.Length < 2)
        {
            es = "0" + es;
        }

        return mant + "e" + sign + es;
    }
}

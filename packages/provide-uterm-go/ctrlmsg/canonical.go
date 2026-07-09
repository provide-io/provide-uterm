//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package ctrlmsg ports two Python modules of provide-uterm to Go:
// provide.uterm.control_channel_builders (typed builder functions returning
// ready-to-encode control messages) and provide.uterm.control_channel_patterns
// (the LinkPattern value object plus its owner-scoped registry).
//
// The builders return map[string]any values ready to hand to
// controlchannel.EncodeControlFrame. Field-presence semantics mirror the
// Python builders exactly (Pydantic exclude_none): optional fields are omitted
// unless explicitly provided.
package ctrlmsg

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// CanonicalJSON serialises v exactly as CPython's
//
//	json.dumps(v, sort_keys=True, separators=(",", ":"))
//
// would (ensure_ascii=True, the default). Reproducing this byte-for-byte is
// load-bearing: make_identity signs a payload that embeds this string, so any
// divergence would break signature parity with the Python producers.
//
// Byte-for-byte parity guarantees:
//   - Object keys are sorted by Unicode code point. Go string comparison is
//     byte-lexicographic over UTF-8, which is identical to code-point order for
//     all scalar values, so sort.Strings matches Python's sorted().
//   - Separators are compact: "," between items and ":" between key and value.
//   - Non-ASCII runes are escaped as \uXXXX (astral runes as a UTF-16 surrogate
//     pair), lowercase hex — never emitted literally (ensure_ascii).
//   - Control characters use the short forms \n \r \t \b \f; all other
//     characters below 0x20 (and 0x7f) use \u00XX.
//   - HTML metacharacters < > & are NOT escaped (Python's json does not escape
//     them; Go's default encoder does, which is why this bespoke encoder
//     exists).
//   - Integers render as plain decimal; floats render via Python's repr()
//     algorithm (see pyFloatRepr); true/false/null use the JSON literals.
//
// Supported Go value types: nil, bool, string, int, int64, float64,
// json.Number, map[string]any and []any. Any other type is an error — claims
// intended for signing must use those types (nested dicts as map[string]any,
// nested lists as []any).
func CanonicalJSON(v any) (string, error) {
	var b strings.Builder
	if err := encodeCanonical(&b, v); err != nil {
		return "", err
	}
	return b.String(), nil
}

func encodeCanonical(b *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		encodeStringCanonical(b, x)
	case int:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case float64:
		b.WriteString(pyFloatRepr(x))
	case json.Number:
		return encodeJSONNumber(b, x)
	case map[string]any:
		return encodeMapCanonical(b, x)
	case []any:
		return encodeSliceCanonical(b, x)
	default:
		return fmt.Errorf("ctrlmsg: cannot canonically encode value of type %T", v)
	}
	return nil
}

// encodeJSONNumber renders a json.Number the way Python's json module would.
// An integer literal (no '.', 'e' or 'E') is emitted verbatim — Go's
// json.Number preserves the source text, which equals CPython str(int) for the
// canonical decimal form produced upstream. A fractional/exponential literal is
// parsed to float64 and re-rendered through pyFloatRepr so the result matches
// Python repr() regardless of how the carrier wrote it.
func encodeJSONNumber(b *strings.Builder, n json.Number) error {
	s := string(n)
	if strings.ContainsAny(s, ".eE") {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return fmt.Errorf("ctrlmsg: invalid json.Number %q: %w", s, err)
		}
		b.WriteString(pyFloatRepr(f))
		return nil
	}
	b.WriteString(s)
	return nil
}

func encodeMapCanonical(b *strings.Builder, m map[string]any) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		encodeStringCanonical(b, k)
		b.WriteByte(':')
		if err := encodeCanonical(b, m[k]); err != nil {
			return err
		}
	}
	b.WriteByte('}')
	return nil
}

func encodeSliceCanonical(b *strings.Builder, s []any) error {
	b.WriteByte('[')
	for i, e := range s {
		if i > 0 {
			b.WriteByte(',')
		}
		if err := encodeCanonical(b, e); err != nil {
			return err
		}
	}
	b.WriteByte(']')
	return nil
}

// encodeStringCanonical writes s as a JSON string using CPython's
// ensure_ascii=True escaping (py_encode_basestring_ascii).
func encodeStringCanonical(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r >= 0x20 && r <= 0x7e:
				b.WriteRune(r)
			case r > 0xffff:
				// Astral plane: emit a UTF-16 surrogate pair.
				v := r - 0x10000
				hi := 0xd800 + (v >> 10)
				lo := 0xdc00 + (v & 0x3ff)
				fmt.Fprintf(b, `\u%04x\u%04x`, hi, lo)
			default:
				fmt.Fprintf(b, `\u%04x`, r)
			}
		}
	}
	b.WriteByte('"')
}

// pyFloatRepr formats f exactly as CPython's repr()/json.dumps would, which is
// the shortest decimal string that round-trips to the same float64, laid out
// with Python's fixed/exponential threshold rules.
//
// Go's strconv produces the same shortest-round-trip digit sequence as
// CPython's dtoa (both are correctly-rounded and the shortest form is unique),
// so we take Go's scientific form to recover the digits and decimal exponent,
// then re-lay them out with Python's placement rules:
//
//	decpt = scientific_exponent + 1     (position of the decimal point)
//	use exponential notation iff decpt <= -4 or decpt > 16
//
// NaN and +/-Infinity render as Python's json literals (NaN, Infinity,
// -Infinity); these are not valid JSON but match json.dumps' default
// allow_nan=True behaviour.
func pyFloatRepr(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}

	// Shortest round-trip scientific form, e.g. "1.5e+00", "1e+16", "-0e+00".
	sci := strconv.FormatFloat(f, 'e', -1, 64)
	neg := false
	if sci[0] == '-' {
		neg = true
		sci = sci[1:]
	}

	ePos := strings.IndexByte(sci, 'e')
	mantissa := sci[:ePos]
	exp, _ := strconv.Atoi(sci[ePos+1:])

	digits := strings.Replace(mantissa, ".", "", 1)
	ndigits := len(digits)
	decpt := exp + 1

	var body string
	if decpt <= -4 || decpt > 16 {
		body = formatExponential(digits, decpt)
	} else {
		body = formatFixed(digits, ndigits, decpt)
	}
	if neg {
		return "-" + body
	}
	return body
}

// formatFixed lays out digits in Python's fixed (non-exponential) notation for
// the given decimal-point position, appending ".0" for integer-valued floats so
// the result is always distinguishable from an int.
func formatFixed(digits string, ndigits, decpt int) string {
	switch {
	case decpt <= 0:
		return "0." + strings.Repeat("0", -decpt) + digits
	case decpt >= ndigits:
		return digits + strings.Repeat("0", decpt-ndigits) + ".0"
	default:
		return digits[:decpt] + "." + digits[decpt:]
	}
}

// formatExponential lays out digits in Python's exponential notation. The
// exponent is signed and zero-padded to a minimum width of two digits.
func formatExponential(digits string, decpt int) string {
	var mant string
	if len(digits) == 1 {
		mant = digits
	} else {
		mant = digits[:1] + "." + digits[1:]
	}
	exp := decpt - 1
	sign := "+"
	if exp < 0 {
		sign = "-"
		exp = -exp
	}
	es := strconv.Itoa(exp)
	if len(es) < 2 {
		es = "0" + es
	}
	return mant + "e" + sign + es
}

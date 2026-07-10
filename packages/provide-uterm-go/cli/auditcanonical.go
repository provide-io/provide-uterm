//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// auditEncode serializes v exactly as CPython's
//
//	json.dumps(v, sort_keys=True, separators=(",",":"), ensure_ascii=False)
//
// would. It differs from ctrlmsg.CanonicalJSON in one axis only — ensure_ascii
// is FALSE here (the audit chain uses it), so non-ASCII runes are emitted
// literally as UTF-8 rather than \uXXXX escaped. It is replicated in this
// package (rather than shared) because the task scopes edits to cli/ + gateway/
// and ctrlmsg's encoder is ensure_ascii=True.
//
// Supported types are exactly those produced by json.Decode with UseNumber:
// nil, bool, string, json.Number, map[string]any and []any.
func auditEncode(b *strings.Builder, v any) error {
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
		auditEncodeString(b, x)
	case json.Number:
		return auditEncodeNumber(b, x)
	case map[string]any:
		return auditEncodeMap(b, x)
	case []any:
		return auditEncodeSlice(b, x)
	default:
		return fmt.Errorf("audit: cannot canonically encode value of type %T", v)
	}
	return nil
}

// auditEncodeNumber renders a json.Number the way Python's json module would: an
// integer literal is emitted verbatim (== str(int)); a fractional/exponential
// literal is parsed to float64 and re-rendered through Python's repr() layout so
// the bytes match json.dumps regardless of the source form.
func auditEncodeNumber(b *strings.Builder, n json.Number) error {
	s := string(n)
	if strings.ContainsAny(s, ".eE") {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return fmt.Errorf("audit: invalid json.Number %q: %w", s, err)
		}
		b.WriteString(pyAuditFloatRepr(f))
		return nil
	}
	b.WriteString(s)
	return nil
}

func auditEncodeMap(b *strings.Builder, m map[string]any) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys) // byte-lexicographic == Python code-point order for scalars
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		auditEncodeString(b, k)
		b.WriteByte(':')
		if err := auditEncode(b, m[k]); err != nil {
			return err
		}
	}
	b.WriteByte('}')
	return nil
}

func auditEncodeSlice(b *strings.Builder, s []any) error {
	b.WriteByte('[')
	for i, e := range s {
		if i > 0 {
			b.WriteByte(',')
		}
		if err := auditEncode(b, e); err != nil {
			return err
		}
	}
	b.WriteByte(']')
	return nil
}

// auditEncodeString writes s as a JSON string using CPython's ensure_ascii=False
// escaping (py_encode_basestring): only ", \, and the C0 control range are
// escaped; 0x7f and all non-ASCII runes pass through literally.
func auditEncodeString(b *strings.Builder, s string) {
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
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// pyAuditFloatRepr formats f exactly as CPython's repr()/json.dumps would (the
// shortest decimal that round-trips, laid out with Python's fixed/exponential
// threshold rules). Go's strconv yields the same shortest digit sequence as
// CPython's dtoa, so we recover the digits from Go's scientific form and re-lay
// them out with Python's placement rules. Replicated from ctrlmsg.pyFloatRepr.
func pyAuditFloatRepr(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
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
		body = auditFormatExponential(digits, decpt)
	} else {
		body = auditFormatFixed(digits, ndigits, decpt)
	}
	if neg {
		return "-" + body
	}
	return body
}

func auditFormatFixed(digits string, ndigits, decpt int) string {
	switch {
	case decpt <= 0:
		return "0." + strings.Repeat("0", -decpt) + digits
	case decpt >= ndigits:
		return digits + strings.Repeat("0", decpt-ndigits) + ".0"
	default:
		return digits[:decpt] + "." + digits[decpt:]
	}
}

func auditFormatExponential(digits string, decpt int) string {
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

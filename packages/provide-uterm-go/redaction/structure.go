//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package redaction

import (
	"fmt"
	"reflect"
	"strings"
)

// RedactMaxDepth is how many container levels RedactStructure walks. Real
// frames are two or three levels deep; the cap keeps a hostile frame nested
// thousands deep from costing unbounded recursion in every recorder that
// redacts it. Mirrors REDACT_MAX_DEPTH in provide.uterm.redaction.
const RedactMaxDepth = 32

// DepthLimitMarker replaces a container at or past the depth cap. The subtree
// is replaced whole, never returned unwalked: an unwalked subtree is an
// unredacted one.
const DepthLimitMarker = "[NESTED_REDACTED]"

// SensitiveKeyMarker replaces the value of a sensitive key under key-aware
// redaction.
const SensitiveKeyMarker = "[SECRET_REDACTED]"

// Key-aware rule, ported from provide.uterm.redaction. A key is split into
// lower-case segments (camelCase split: accessToken -> access, token; APIKey ->
// api, key; "-", "." and " " act like "_"). It is sensitive when any segment is
// in sensitiveKeySegments, or two adjacent segments form a pair in
// sensitiveKeyPairs -- unless its last segment is in nonSecretKeySuffixes
// (token_count, token_type describe a credential rather than hold one).
var sensitiveKeySegments = map[string]bool{
	"token":         true,
	"secret":        true,
	"password":      true,
	"passwd":        true,
	"passphrase":    true,
	"credential":    true,
	"credentials":   true,
	"cookie":        true,
	"authorization": true,
	"apikey":        true,
}

var sensitiveKeyPairs = map[[2]string]bool{
	{"api", "key"}:     true,
	{"private", "key"}: true,
	{"access", "key"}:  true,
}

var nonSecretKeySuffixes = map[string]bool{
	"count": true, "len": true, "length": true, "size": true, "type": true, "kind": true,
	"ttl": true, "expiry": true, "expires": true, "hash": true, "required": true, "enabled": true,
}

func isASCIIUpper(c byte) bool { return c >= 'A' && c <= 'Z' }

func isASCIILower(c byte) bool { return c >= 'a' && c <= 'z' }

func isASCIILowerOrDigit(c byte) bool { return isASCIILower(c) || (c >= '0' && c <= '9') }

// KeySegments splits key into lower-case segments, exactly as the reference
// key_segments does.
func KeySegments(key string) []string {
	var b strings.Builder
	last := len(key) - 1
	for i := 0; i < len(key); i++ {
		c := key[i]
		if i > 0 && isASCIIUpper(c) {
			prev := key[i-1]
			endsAcronym := isASCIIUpper(prev) && i < last && isASCIILower(key[i+1])
			if isASCIILowerOrDigit(prev) || endsAcronym {
				b.WriteByte('_')
			}
		}
		b.WriteByte(c)
	}
	folded := strings.NewReplacer("-", "_", ".", "_", " ", "_").Replace(strings.ToLower(b.String()))
	segments := []string{}
	for _, segment := range strings.Split(folded, "_") {
		if segment != "" {
			segments = append(segments, segment)
		}
	}
	return segments
}

// IsSensitiveKey reports whether key names a credential field (see the rule
// above sensitiveKeySegments).
func IsSensitiveKey(key string) bool {
	segments := KeySegments(key)
	if len(segments) == 0 || nonSecretKeySuffixes[segments[len(segments)-1]] {
		return false
	}
	for i, segment := range segments {
		if sensitiveKeySegments[segment] {
			return true
		}
		if i > 0 && sensitiveKeyPairs[[2]string{segments[i-1], segment}] {
			return true
		}
	}
	return false
}

// StructureOptions tune RedactStructure.
type StructureOptions struct {
	// KeyAware masks the value of every sensitive key (see IsSensitiveKey)
	// with SensitiveKeyMarker, unless the value is nil.
	KeyAware bool
	// MaxDepth overrides the depth cap when non-nil (0 caps at the root,
	// exactly like the reference's max_depth=0); nil selects RedactMaxDepth.
	MaxDepth *int
}

// RedactStructure returns a redacted copy of a JSON-like value. Port of
// provide.uterm.redaction.redact_structure:
//
//   - strings go through r at any depth;
//   - maps and slices/arrays are rebuilt as map[string]any / []any (keys kept,
//     the input never mutated); []byte is a scalar here, as encoding/json
//     treats it;
//   - a container MaxDepth levels below the root (the root is level 0) is
//     replaced by DepthLimitMarker;
//   - with KeyAware, a value under a sensitive key becomes SensitiveKeyMarker
//     unless it is nil -- untyped, or a typed nil pointer/map/slice/interface,
//     the shapes that encode as JSON null (the reference keeps None);
//   - every other value is returned unchanged.
func RedactStructure(value any, r Redactor, opts StructureOptions) any {
	maxDepth := RedactMaxDepth
	if opts.MaxDepth != nil {
		maxDepth = *opts.MaxDepth
	}
	w := walker{redact: r, keyAware: opts.KeyAware, maxDepth: maxDepth}
	return w.walk(value, 0)
}

type walker struct {
	redact   Redactor
	keyAware bool
	maxDepth int
}

func (w walker) walk(value any, depth int) any {
	switch v := value.(type) {
	case string:
		return w.redact(v)
	case []byte:
		return v
	}
	rv := reflect.ValueOf(value)
	kind := rv.Kind()
	if kind != reflect.Map && kind != reflect.Slice && kind != reflect.Array {
		return value
	}
	if kind != reflect.Array && rv.IsNil() {
		return value
	}
	if depth >= w.maxDepth {
		return DepthLimitMarker
	}
	if kind == reflect.Map {
		out := make(map[string]any, rv.Len())
		iter := rv.MapRange()
		for iter.Next() {
			key := fmt.Sprint(iter.Key().Interface())
			out[key] = w.entry(key, iter.Value().Interface(), depth+1)
		}
		return out
	}
	out := make([]any, rv.Len())
	for i := range out {
		out[i] = w.walk(rv.Index(i).Interface(), depth+1)
	}
	return out
}

func (w walker) entry(key string, item any, depth int) any {
	if w.keyAware && !isNil(item) && IsSensitiveKey(key) {
		return SensitiveKeyMarker
	}
	return w.walk(item, depth)
}

// isNil reports whether v is nil or a typed nil that encodes as JSON null.
func isNil(v any) bool {
	if v == nil {
		return true
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Pointer, reflect.Map, reflect.Slice, reflect.Interface, reflect.Func, reflect.Chan:
		return rv.IsNil()
	}
	return false
}

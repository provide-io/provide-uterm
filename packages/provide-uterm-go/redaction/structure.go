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

// sensitiveKeyNames are field names whose value is a credential regardless of
// what it looks like: the text rules need context such as `token=` to fire,
// and a bare {"token": "..."} has none.
var sensitiveKeyNames = map[string]bool{
	"token":         true,
	"password":      true,
	"passwd":        true,
	"passphrase":    true,
	"secret":        true,
	"api_key":       true,
	"apikey":        true,
	"authorization": true,
	"cookie":        true,
	"private_key":   true,
}

// sensitiveKeySuffixes mark any key ending in them as sensitive too
// (resume_token, client_secret, ssh_passphrase, ...).
var sensitiveKeySuffixes = []string{"_token", "_password", "_passphrase", "_secret", "_api_key"}

// IsSensitiveKey reports whether key names a credential field. Matching is on
// the lower-cased key with "-" folded to "_".
func IsSensitiveKey(key string) bool {
	folded := strings.ReplaceAll(strings.ToLower(key), "-", "_")
	if sensitiveKeyNames[folded] {
		return true
	}
	for _, suffix := range sensitiveKeySuffixes {
		if strings.HasSuffix(folded, suffix) {
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
	// MaxDepth overrides the depth cap; zero or negative selects
	// RedactMaxDepth.
	MaxDepth int
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
//   - with KeyAware, a non-nil value under a sensitive key becomes
//     SensitiveKeyMarker;
//   - every other value is returned unchanged.
func RedactStructure(value any, r Redactor, opts StructureOptions) any {
	maxDepth := opts.MaxDepth
	if maxDepth <= 0 {
		maxDepth = RedactMaxDepth
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
	if w.keyAware && item != nil && IsSensitiveKey(key) {
		return SensitiveKeyMarker
	}
	return w.walk(item, depth)
}

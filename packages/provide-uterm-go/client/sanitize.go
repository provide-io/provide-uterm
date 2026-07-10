//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import "strings"

// sanitizeMaxString is the length above which a string is truncated in log
// output; sanitizeMaxList is the number of list elements kept before eliding
// the rest. Both mirror the Python client's _sanitize helper.
const (
	sanitizeMaxString = 500
	sanitizeMaxList   = 10
)

// sensitiveKeySubstrings are the case-insensitive substrings that mark a map
// key whose value must be redacted before it reaches the logs. This is the
// exact set the Python _sanitize helper uses.
var sensitiveKeySubstrings = []string{"token", "secret", "password", "key", "auth", "session_id"}

// sanitize deeply strips sensitive values and truncates long strings/lists so
// that a decoded response body is safe to log. It is a direct port of
// provide.uterm.client.hijack._sanitize.
//
//   - map values are redacted to "***" when the key contains a sensitive
//     substring; otherwise the value is sanitized recursively;
//   - slices longer than sanitizeMaxList keep the first sanitizeMaxList
//     elements followed by "...";
//   - strings longer than sanitizeMaxString are truncated with a "..." suffix.
func sanitize(data any) any {
	switch v := data.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, val := range v {
			if isSensitiveKey(k) {
				out[k] = "***"
			} else {
				out[k] = sanitize(val)
			}
		}
		return out
	case []any:
		if len(v) > sanitizeMaxList {
			out := make([]any, 0, sanitizeMaxList+1)
			for _, x := range v[:sanitizeMaxList] {
				out = append(out, sanitize(x))
			}
			out = append(out, "...")
			return out
		}
		out := make([]any, 0, len(v))
		for _, x := range v {
			out = append(out, sanitize(x))
		}
		return out
	case string:
		if len(v) > sanitizeMaxString {
			return v[:sanitizeMaxString] + "..."
		}
		return v
	default:
		return data
	}
}

// isSensitiveKey reports whether key contains any sensitive substring
// (case-insensitive), matching the Python _sanitize key check.
func isSensitiveKey(key string) bool {
	lower := strings.ToLower(key)
	for _, s := range sensitiveKeySubstrings {
		if strings.Contains(lower, s) {
			return true
		}
	}
	return false
}

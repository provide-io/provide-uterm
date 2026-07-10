//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"encoding/base64"
	"fmt"
	"strings"
)

// bodyMaxBytes is the largest body echoed inline (256 KiB), matching
// http_proxy.py BODY_MAX_BYTES.
const bodyMaxBytes = 256 * 1024

// binaryContentTypes are content-type prefixes treated as opaque binary (the
// body is summarized, never base64-inlined). Mirrors BINARY_CONTENT_TYPES.
var binaryContentTypes = []string{
	"image/",
	"audio/",
	"video/",
	"application/octet-stream",
	"application/zip",
	"application/gzip",
	"application/pdf",
	"application/wasm",
	"font/",
}

// isBinaryContentType reports whether content-type denotes binary content.
// It lowercases, drops any ";" parameters and trims, matching Python's _is_binary.
func isBinaryContentType(contentType string) bool {
	ct := strings.ToLower(contentType)
	if i := strings.IndexByte(ct, ';'); i >= 0 {
		ct = ct[:i]
	}
	ct = strings.TrimSpace(ct)
	for _, prefix := range binaryContentTypes {
		if strings.HasPrefix(ct, prefix) {
			return true
		}
	}
	return false
}

// EncodeBody encodes a request/response body per the inspection spec, returning
// a map with the same keys Python's encode_body produces so the JSON emitted on
// ChannelHTTP is identical:
//
//   - always: body_size
//   - empty body: only body_size
//   - binary content-type: body_binary=true
//   - too large: body_truncated=true
//   - otherwise: body_b64 (base64 of the body)
func EncodeBody(body []byte, contentType string) map[string]any {
	result := map[string]any{"body_size": len(body)}
	if len(body) == 0 {
		return result
	}
	if isBinaryContentType(contentType) {
		result["body_binary"] = true
		return result
	}
	if len(body) > bodyMaxBytes {
		result["body_truncated"] = true
		return result
	}
	result["body_b64"] = base64.StdEncoding.EncodeToString(body)
	return result
}

// FormatLogLine formats a compact mitmproxy-style log line. status < 0 means "no
// status yet" (the request phase), mirroring Python's status=None. durationMs < 0
// means "unknown" and renders as "?".
func FormatLogLine(method, url string, status int, durationMs float64, bodySize int) string {
	sizeStr := humanSize(bodySize)
	if status < 0 {
		return fmt.Sprintf("→ %s %s (%s)", method, url, sizeStr)
	}
	warn := ""
	if status >= 500 {
		warn = " ⚠"
	}
	dur := "?"
	if durationMs >= 0 {
		dur = fmt.Sprintf("%.0fms", durationMs)
	}
	return fmt.Sprintf("← %d %s %s (%s, %s)%s", status, method, url, dur, sizeStr, warn)
}

// humanSize renders a byte count the way Python's _human_size does.
func humanSize(n int) string {
	switch {
	case n < 1024:
		return fmt.Sprintf("%dB", n)
	case n < 1024*1024:
		return fmt.Sprintf("%.1fKB", float64(n)/1024)
	default:
		return fmt.Sprintf("%.1fMB", float64(n)/(1024*1024))
	}
}

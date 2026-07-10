//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// Input-hardening validators and snapshot shaping for the MCP tool surface.
// Port of provide.uterm.ai.server_validators: path-segment id validation,
// ReDoS-guarded regex compilation, connector-config vetting, and snapshot
// output shaping. Every validator returns the same structured rejection dicts
// (and byte-for-byte error detail strings) as the reference implementation.

// safeIDPattern matches a single safe URL path segment. Mirrors _ID_RE in
// hijack.py and safeIDPattern in the Go client.
var safeIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// pyStrRepr renders s the way Python's repr() does for a str: single quotes
// unless the string contains a single quote but no double quote, with the
// standard control-character escapes. Used to reproduce the _safe_id error
// message byte-for-byte.
func pyStrRepr(s string) string {
	quote := byte('\'')
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\\':
			b.WriteString(`\\`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// checkSafeID validates a caller/LLM-supplied path-segment id, returning the
// Python _safe_id ValueError message on failure.
func checkSafeID(value, kind string) error {
	if value == "" || value == "." || value == ".." || !safeIDPattern.MatchString(value) {
		return fmt.Errorf("invalid %s: %s", kind, pyStrRepr(value))
	}
	return nil
}

// rejectBadID validates a single path-segment id, returning the structured
// invalid_id rejection dict or nil.
func rejectBadID(value, kind string) map[string]any {
	if err := checkSafeID(value, kind); err != nil {
		return map[string]any{"success": false, "error": "invalid_id", "detail": err.Error()}
	}
	return nil
}

// idPair is a (value, kind) pair for rejectBadIDs.
type idPair struct{ value, kind string }

// rejectBadIDs validates several path-segment ids in order, returning the first
// rejection or nil.
func rejectBadIDs(pairs ...idPair) map[string]any {
	for _, p := range pairs {
		if r := rejectBadID(p.value, p.kind); r != nil {
			return r
		}
	}
	return nil
}

// compileUserPattern compiles an attacker-supplied regex behind a length +
// structural guard, returning the byte-for-byte Python error messages. Note the
// compile step uses Go's RE2 engine, so the "invalid pattern: ..." detail text
// for a malformed pattern differs from Python's re.error wording.
func compileUserPattern(pattern string) (*regexp.Regexp, error) {
	if utf8.RuneCountInString(pattern) > MaxUserPatternLen {
		return nil, fmt.Errorf("pattern too long (max %d chars)", MaxUserPatternLen)
	}
	if hasCatastrophicConstruct(pattern) {
		return nil, fmt.Errorf(
			"pattern rejected: catastrophic-backtracking construct " +
				"(nested quantifier or quantified backreference)")
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil, fmt.Errorf("invalid pattern: %w", err)
	}
	return re, nil
}

// rejectBadPattern validates a user-supplied regex, returning the invalid_pattern
// rejection dict or nil. A nil pattern is allowed (no filter requested).
func rejectBadPattern(pattern *string) map[string]any {
	if pattern == nil {
		return nil
	}
	if _, err := compileUserPattern(*pattern); err != nil {
		return map[string]any{"success": false, "error": "invalid_pattern", "detail": err.Error()}
	}
	return nil
}

// compiledPatternOrRejection compiles a user pattern once, returning
// (compiled, nil) on success, (nil, rejection) for a bad pattern, or (nil, nil)
// when no pattern is requested.
func compiledPatternOrRejection(pattern *string) (*regexp.Regexp, map[string]any) {
	if pattern == nil {
		return nil, nil
	}
	re, err := compileUserPattern(*pattern)
	if err != nil {
		return nil, map[string]any{"success": false, "error": "invalid_pattern", "detail": err.Error()}
	}
	return re, nil
}

// pyLineBoundary reports whether r is a line boundary for Python str.splitlines.
func pyLineBoundary(r rune) bool {
	switch r {
	case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
		return true
	}
	return false
}

// pySplitlines splits s on line boundaries the way Python str.splitlines()
// does (without keepends): \r\n counts as one boundary, and no trailing empty
// element is produced.
func pySplitlines(s string) []string {
	var lines []string
	runes := []rune(s)
	start, i, n := 0, 0, len(runes)
	for i < n {
		if pyLineBoundary(runes[i]) {
			lines = append(lines, string(runes[start:i]))
			if runes[i] == '\r' && i+1 < n && runes[i+1] == '\n' {
				i++
			}
			i++
			start = i
			continue
		}
		i++
	}
	if start < n {
		lines = append(lines, string(runes[start:]))
	}
	return lines
}

// trimTail trims screen to the last tailLines lines (no-op when tailLines is
// nil or non-positive).
func trimTail(screenText string, tailLines *int) string {
	if tailLines != nil && *tailLines > 0 {
		lines := pySplitlines(screenText)
		if len(lines) > *tailLines {
			return strings.Join(lines[len(lines)-*tailLines:], "\n")
		}
	}
	return screenText
}

// screenField extracts the "screen" text from a snapshot, defaulting to "".
func screenField(snapshot map[string]any) string {
	if s, ok := snapshot["screen"].(string); ok {
		return s
	}
	return ""
}

// cleanSnapshot processes a snapshot dict according to the requested output
// mode ("text", "rendered", or "raw"), optionally trimming to tailLines.
func cleanSnapshot(snapshot map[string]any, output string, tailLines *int) map[string]any {
	if output == "raw" {
		if tailLines != nil && *tailLines > 0 {
			lines := pySplitlines(screenField(snapshot))
			if len(lines) > *tailLines {
				out := make(map[string]any, len(snapshot))
				for k, v := range snapshot {
					out[k] = v
				}
				out["screen"] = strings.Join(lines[len(lines)-*tailLines:], "\n")
				return out
			}
		}
		return snapshot
	}
	screenText := trimTail(screen.StripANSI(screenField(snapshot)), tailLines)
	if output == "text" {
		return map[string]any{"screen": screenText}
	}
	// rendered: visual grid intact, ANSI stripped, include layout metadata.
	result := map[string]any{"screen": screenText}
	for _, key := range []string{"cursor", "cols", "rows"} {
		if v, ok := snapshot[key]; ok {
			result[key] = v
		}
	}
	return result
}

// validateSessionCreateConfig vets a session_create request against the
// connector allowlist, port range, url scheme, and SSRF host classification.
// Returns nil when acceptable, or the matching structured rejection dict.
func validateSessionCreateConfig(connectorType string, urlArg *string, port *int, host *string) map[string]any {
	if !isAllowedConnector(connectorType) {
		return map[string]any{"success": false, "error": "invalid_connector_type", "connector_type": connectorType}
	}
	if port != nil && (*port < 1 || *port > 65535) {
		return map[string]any{"success": false, "error": "invalid_port", "port": *port}
	}
	if urlArg != nil {
		scheme := ""
		if strings.Contains(*urlArg, "://") {
			scheme = strings.ToLower(strings.SplitN(*urlArg, "://", 2)[0])
		}
		switch scheme {
		case "ws", "wss", "http", "https", "telnet", "ssh":
		default:
			reported := scheme
			if reported == "" {
				reported = "<missing>"
			}
			return map[string]any{"success": false, "error": "invalid_url_scheme", "scheme": reported}
		}
		if parsedHost := urlHostname(*urlArg); parsedHost != "" && isInternalHost(parsedHost) {
			return map[string]any{"success": false, "error": "invalid_host", "host": parsedHost}
		}
	}
	if host != nil && isInternalHost(*host) {
		return map[string]any{"success": false, "error": "invalid_host", "host": *host}
	}
	return nil
}

// urlHostname returns the lowercased hostname of a URL (no port, no brackets),
// matching urllib.parse.urlparse(url).hostname. Returns "" when absent or the
// URL cannot be parsed.
func urlHostname(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

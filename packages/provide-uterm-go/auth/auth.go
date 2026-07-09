//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package auth provides pluggable SSH-key-based authentication for
// provide-uterm gateways. Port of provide.uterm.auth.
//
// It defines the boundary between transport (the SSH handshake and pubkey
// fingerprint, owned by provide-uterm) and identity (who the key belongs to,
// owned by the consuming application). Consumers plug in an SSHKeyResolver
// mapping a fingerprint to a ResolvedIdentity; on a hit the identity is
// threaded through the downstream WebSocket via a control-channel identity
// frame.
//
// Trust model: when a proxy asserts an identity to the upstream server, the
// server is trusting the proxy to have done the auth correctly. This is a
// pure bearer-trust model — upstreams should only opt in when the proxy is
// under the same operational control; otherwise ignore identity frames and
// re-authenticate independently.
package auth

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"strings"
	"unicode"
)

// ResolvedIdentity is an identity successfully resolved from an SSH public
// key. Subject is an opaque consumer-defined identifier (e.g. "player:42");
// Claims carry additional attributes; Fingerprint is the OpenSSH-style
// SHA256 fingerprint that was resolved.
type ResolvedIdentity struct {
	Subject     string
	Claims      map[string]any
	Fingerprint string
}

// SSHKeyResolver maps an SSH public key to an application identity.
// Implementations are called once per inbound SSH connection during
// public-key auth. Return (nil, nil) to signal "this key is not known" —
// the gateway then falls through to password auth or rejects, depending on
// its require-resolver setting.
type SSHKeyResolver interface {
	Resolve(ctx context.Context, fingerprint string, pubkeyBlob []byte, username string) (*ResolvedIdentity, error)
}

// NullResolver never resolves anything — equivalent to not configuring a
// resolver at all, so callers can pass a non-nil resolver unconditionally.
type NullResolver struct{}

// Resolve implements SSHKeyResolver; it always returns (nil, nil).
func (NullResolver) Resolve(context.Context, string, []byte, string) (*ResolvedIdentity, error) {
	return nil, nil
}

// FingerprintFromOpenSSHBlob computes an OpenSSH-style SHA256 fingerprint
// from raw key bytes. It accepts either the binary SSH wire format or the
// text form starting with "ssh-"/"ecdsa-"/"sk-", and returns a string like
// "SHA256:1234…" matching ssh-keygen -lf output.
func FingerprintFromOpenSSHBlob(blob []byte) (string, error) {
	binary, err := coerceToBinaryPubkey(blob)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(binary)
	b64 := strings.TrimRight(base64.StdEncoding.EncodeToString(digest[:]), "=")
	return "SHA256:" + b64, nil
}

var textKeyPrefixes = []string{"ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"}

func hasKeytypePrefix(s string) bool {
	for _, prefix := range textKeyPrefixes {
		if strings.HasPrefix(s, prefix) {
			return true
		}
	}
	return false
}

// coerceToBinaryPubkey extracts the base64-decoded SSH wire-format bytes
// from blob, handling both the OpenSSH text form ("ssh-ed25519 AAAAC3…") and
// raw wire bytes (passed through unchanged).
func coerceToBinaryPubkey(blob []byte) ([]byte, error) {
	stripped := strings.TrimSpace(string(blob))
	if hasKeytypePrefix(stripped) {
		parts := strings.Fields(stripped)
		if len(parts) < 2 {
			return nil, errors.New("malformed OpenSSH public key line")
		}
		decoded, err := base64.StdEncoding.Strict().DecodeString(parts[1])
		if err != nil {
			return nil, fmt.Errorf("invalid base64 in public key: %w", err)
		}
		return decoded, nil
	}
	return []byte(stripped), nil
}

// AuthorizedKeysFileResolver resolves identities against a file in OpenSSH
// authorized_keys format. Recognised options: subject="…" (explicit subject;
// defaults to the comment, or "key:<fp>") and claim-<name>="…" entries.
// Unrecognised OpenSSH options are preserved in claims under "_options".
//
// The file is read and parsed on each Resolve call, so key rotation picks up
// immediately; wrap in a caching resolver for huge files.
type AuthorizedKeysFileResolver struct {
	path string
}

// NewAuthorizedKeysFileResolver creates a resolver over the given
// authorized_keys file path.
func NewAuthorizedKeysFileResolver(path string) *AuthorizedKeysFileResolver {
	return &AuthorizedKeysFileResolver{path: path}
}

// Resolve implements SSHKeyResolver by fingerprint match against the file.
func (r *AuthorizedKeysFileResolver) Resolve(
	_ context.Context, fingerprint string, _ []byte, _ string,
) (*ResolvedIdentity, error) {
	for _, entry := range r.loadEntries() {
		if entry.fingerprint == fingerprint {
			return &ResolvedIdentity{
				Subject:     entry.subject,
				Claims:      entry.claims,
				Fingerprint: fingerprint,
			}, nil
		}
	}
	return nil, nil
}

type authorizedKeyEntry struct {
	fingerprint string
	subject     string
	claims      map[string]any
}

// loadEntries parses the file, skipping malformed lines rather than aborting
// the whole file — one bad entry shouldn't lock everybody out. A missing
// file yields no entries.
func (r *AuthorizedKeysFileResolver) loadEntries() []authorizedKeyEntry {
	raw, err := os.ReadFile(r.path)
	if err != nil {
		return nil
	}
	var out []authorizedKeyEntry
	for _, rawLine := range strings.Split(string(raw), "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		entry, err := parseAuthorizedKeysLine(line)
		if err != nil {
			continue
		}
		out = append(out, entry)
	}
	return out
}

// parseAuthorizedKeysLine parses one non-empty/non-comment line following
// the simplified grammar: [options_csv] keytype base64_payload [comment...].
// Options are detected by the first token not matching a keytype prefix.
func parseAuthorizedKeysLine(line string) (authorizedKeyEntry, error) {
	firstTokenEnd := findFirstTokenEnd(line)
	firstToken := line[:firstTokenEnd]

	var optionsStr, rest string
	if hasKeytypePrefix(firstToken) {
		rest = line
	} else {
		optionsStr = firstToken
		rest = strings.TrimLeft(line[firstTokenEnd:], " \t")
	}

	fields := strings.Fields(rest)
	if len(fields) < 2 {
		return authorizedKeyEntry{}, errors.New("missing key payload")
	}
	keytype, payload := fields[0], fields[1]
	comment := ""
	if len(fields) > 2 {
		// Preserve the raw comment tail (may contain spaces).
		idx := strings.Index(rest, payload) + len(payload)
		comment = strings.TrimSpace(rest[idx:])
	}

	fp, err := FingerprintFromOpenSSHBlob([]byte(keytype + " " + payload))
	if err != nil {
		return authorizedKeyEntry{}, err
	}

	opts := map[string]any{}
	if optionsStr != "" {
		opts = parseOptions(optionsStr)
	}

	subject := ""
	if s, ok := opts["subject"].(string); ok && s != "" {
		subject = s
	}
	delete(opts, "subject")
	if subject == "" {
		subject = comment
	}
	if subject == "" {
		subject = "key:" + fp
	}

	claims := map[string]any{}
	leftover := map[string]any{}
	for key, value := range opts {
		if strings.HasPrefix(key, "claim-") {
			claims[strings.TrimPrefix(key, "claim-")] = value
		} else {
			leftover[key] = value
		}
	}
	if len(leftover) > 0 {
		claims["_options"] = leftover
	}

	return authorizedKeyEntry{fingerprint: fp, subject: subject, claims: claims}, nil
}

// findFirstTokenEnd returns the index of the first top-level whitespace,
// respecting double-quoted substrings so `command="echo hi",no-pty` is a
// single token. Backslash escapes are NOT interpreted — OpenSSH's own parser
// doesn't either inside option values.
func findFirstTokenEnd(line string) int {
	inQuotes := false
	for i, ch := range line {
		switch {
		case ch == '"':
			inQuotes = !inQuotes
		case unicode.IsSpace(ch) && !inQuotes:
			return i
		}
	}
	return len(line)
}

// parseOptions parses the comma-separated OpenSSH options field:
// key="value" → "value" (string); bare flag → true (bool).
func parseOptions(optionsStr string) map[string]any {
	out := map[string]any{}
	for _, token := range splitOptions(optionsStr) {
		if key, value, found := strings.Cut(token, "="); found {
			out[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), `"`)
		} else {
			out[strings.TrimSpace(token)] = true
		}
	}
	return out
}

// splitOptions splits an options CSV string on commas that aren't inside
// quotes.
func splitOptions(optionsStr string) []string {
	var out []string
	var buf strings.Builder
	inQuotes := false
	for _, ch := range optionsStr {
		switch {
		case ch == '"':
			inQuotes = !inQuotes
			buf.WriteRune(ch)
		case ch == ',' && !inQuotes:
			if buf.Len() > 0 {
				out = append(out, buf.String())
				buf.Reset()
			}
		default:
			buf.WriteRune(ch)
		}
	}
	if buf.Len() > 0 {
		out = append(out, buf.String())
	}
	return out
}

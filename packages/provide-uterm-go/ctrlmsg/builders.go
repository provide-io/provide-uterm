//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
)

// identityVersion is the fixed protocol version stamped into every identity
// message (Pydantic IdentityFrame.version default).
const identityVersion = 1

// identityConfig accumulates the optional arguments of MakeIdentity.
type identityConfig struct {
	claims      map[string]any
	hasClaims   bool
	fingerprint string
	transport   string
	secret      []byte
}

// IdentityOption configures MakeIdentity. Unset options fall back to the Python
// builder defaults: no claims key, fingerprint="", transport="ssh", unsigned.
type IdentityOption func(*identityConfig)

// WithClaims attaches an additional-claims mapping. Passing this option (even
// with a nil/empty map) makes the "claims" key present in the output, matching
// Python make_identity(claims={...}); omitting it leaves "claims" absent,
// matching claims=None.
func WithClaims(claims map[string]any) IdentityOption {
	return func(c *identityConfig) {
		c.claims = claims
		c.hasClaims = true
	}
}

// WithFingerprint sets the SSH key fingerprint field.
func WithFingerprint(fingerprint string) IdentityOption {
	return func(c *identityConfig) { c.fingerprint = fingerprint }
}

// WithTransport overrides the transport field (default "ssh").
func WithTransport(transport string) IdentityOption {
	return func(c *identityConfig) { c.transport = transport }
}

// WithSecret enables HMAC signing with the given secret. An empty secret
// (len 0) leaves the message unsigned, matching Python's falsy-secret check.
func WithSecret(secret []byte) IdentityOption {
	return func(c *identityConfig) { c.secret = secret }
}

// MakeIdentity builds an "identity" control message, mirroring the Python
// make_identity builder.
//
// When a non-empty secret is supplied the message carries a "signature" field:
// the lowercase hex HMAC-SHA256 of the canonical payload
//
//	"{version}:{subject}:{fingerprint}:{transport}:{claims_json}"
//
// where claims_json is CanonicalJSON(claims) — "{}" when no claims were
// provided. An empty subject is an error.
func MakeIdentity(subject string, opts ...IdentityOption) (map[string]any, error) {
	if subject == "" {
		return nil, fmt.Errorf("make_identity: 'subject' must be a non-empty string")
	}
	cfg := identityConfig{transport: "ssh"}
	for _, opt := range opts {
		opt(&cfg)
	}

	msg := map[string]any{
		"type":        "identity",
		"version":     identityVersion,
		"subject":     subject,
		"fingerprint": cfg.fingerprint,
		"transport":   cfg.transport,
	}
	if cfg.hasClaims {
		msg["claims"] = copyStringMap(cfg.claims)
	}

	if len(cfg.secret) > 0 {
		claimsForSig := cfg.claims
		if claimsForSig == nil {
			claimsForSig = map[string]any{}
		}
		claimsJSON, err := CanonicalJSON(claimsForSig)
		if err != nil {
			return nil, fmt.Errorf("make_identity: cannot encode claims: %w", err)
		}
		payload := fmt.Sprintf("%d:%s:%s:%s:%s", identityVersion, subject, cfg.fingerprint, cfg.transport, claimsJSON)
		mac := hmac.New(sha256.New, cfg.secret)
		mac.Write([]byte(payload))
		msg["signature"] = hex.EncodeToString(mac.Sum(nil))
	}

	return msg, nil
}

// copyStringMap returns a shallow copy of m (never nil), matching Python's
// dict(claims) so a caller mutating the returned message cannot reach back into
// the argument.
func copyStringMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// MakeSessionToken builds a "session_token" control message. An empty token is
// an error; the "player_id" key is present only when playerID is non-nil (so a
// player_id of 0 is still emitted).
func MakeSessionToken(token string, playerID *int) (map[string]any, error) {
	if token == "" {
		return nil, fmt.Errorf("make_session_token: 'token' must be a non-empty string")
	}
	msg := map[string]any{"type": "session_token", "token": token}
	if playerID != nil {
		msg["player_id"] = *playerID
	}
	return msg, nil
}

// MakeResume builds a "resume" control message. An empty token is an error; the
// "player_id" key is present only when playerID is non-nil.
func MakeResume(token string, playerID *int) (map[string]any, error) {
	if token == "" {
		return nil, fmt.Errorf("make_resume: 'token' must be a non-empty string")
	}
	msg := map[string]any{"type": "resume", "token": token}
	if playerID != nil {
		msg["player_id"] = *playerID
	}
	return msg, nil
}

// MakeResumeOk builds a "resume_ok" control message.
func MakeResumeOk() map[string]any {
	return map[string]any{"type": "resume_ok"}
}

// MakeResumeFailed builds a "resume_failed" control message. The "reason" key
// is omitted when reason is nil and included (even for the empty string) when
// reason is non-nil.
func MakeResumeFailed(reason *string) map[string]any {
	msg := map[string]any{"type": "resume_failed"}
	if reason != nil {
		msg["reason"] = *reason
	}
	return msg
}

// linkPatternValidators maps each allowed link-pattern entry key to a validator
// returning an error string ("" == valid). Keys absent from this map are
// unknown fields and rejected.
var linkPatternFields = map[string]func(any) string{
	"pattern":       validateStringField,
	"action":        validateActionField,
	"id":            validateStringField,
	"flags":         validateStringField,
	"group":         validateGroupField,
	"payload":       func(any) string { return "" }, // any value permitted
	"hover":         validateStringField,
	"line_contains": validateStringField,
	"class":         validateStringField,
}

var validLinkActions = []string{"cmd", "focus", "key", "url"}

func validateStringField(v any) string {
	if _, ok := v.(string); !ok {
		return "must be a string"
	}
	return ""
}

func validateActionField(v any) string {
	s, ok := v.(string)
	if !ok {
		return "must be a string"
	}
	if !isValidLinkAction(s) {
		return fmt.Sprintf("is invalid (%q); must be one of [%s]", s, strings.Join(validLinkActions, " "))
	}
	return ""
}

func validateGroupField(v any) string {
	switch v.(type) {
	case int, int64, string:
		return ""
	default:
		return "must be an int or string"
	}
}

func isValidLinkAction(s string) bool {
	for _, a := range validLinkActions {
		if a == s {
			return true
		}
	}
	return false
}

// MakeLinkPatterns builds a "link_patterns" control message from a sequence of
// pattern entries, mirroring the Python make_link_patterns builder. Each entry
// is validated against the LinkPatternEntry field set: "pattern" (string) and
// "action" (one of cmd/url/key/focus) are required; "id", "flags", "group"
// (int or string), "payload" (any), "hover", "line_contains" and "class" are
// optional. An unknown or mistyped field is an error mentioning the entry index.
func MakeLinkPatterns(patterns []map[string]any) (map[string]any, error) {
	entries := make([]any, 0, len(patterns))
	for i, entry := range patterns {
		validated, err := validateLinkPatternEntry(entry)
		if err != nil {
			return nil, fmt.Errorf("make_link_patterns: entry[%d] is invalid: %s", i, err)
		}
		entries = append(entries, validated)
	}
	return map[string]any{"type": "link_patterns", "patterns": entries}, nil
}

// validateLinkPatternEntry returns a fresh map of the valid keys of entry, or
// an error describing the first problem found (deterministic order: unknown
// fields, then required fields, then optional-field types).
func validateLinkPatternEntry(entry map[string]any) (map[string]any, error) {
	// Reject unknown fields first, in sorted order for determinism.
	keys := make([]string, 0, len(entry))
	for k := range entry {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if _, ok := linkPatternFields[k]; !ok {
			return nil, fmt.Errorf("unknown field %q", k)
		}
	}

	// Required fields.
	if _, ok := entry["pattern"]; !ok {
		return nil, fmt.Errorf("field \"pattern\" is required")
	}
	if _, ok := entry["action"]; !ok {
		return nil, fmt.Errorf("field \"action\" is required")
	}

	// Type validation for every present field.
	out := make(map[string]any, len(entry))
	for _, k := range keys {
		v := entry[k]
		if msg := linkPatternFields[k](v); msg != "" {
			return nil, fmt.Errorf("field %q %s", k, msg)
		}
		out[k] = v
	}
	return out, nil
}

// MakePresenceUpdate builds a "presence_update" control message for userID,
// merging arbitrary extra fields. Nil-valued fields are dropped, mirroring the
// Python builder's exclude_none dump.
func MakePresenceUpdate(userID string, fields map[string]any) map[string]any {
	msg := map[string]any{"type": "presence_update", "user_id": userID}
	for k, v := range fields {
		if v == nil {
			continue
		}
		msg[k] = v
	}
	return msg
}

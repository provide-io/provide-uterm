//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// ResolvedIdentity mirrors provide.uterm.auth.ResolvedIdentity: an opaque,
// consumer-defined subject plus free-form claims and an SSH fingerprint.
type ResolvedIdentity struct {
	Subject     string
	Claims      map[string]any
	Fingerprint string
}

// supportedIdentityVersions is the frozenset of understood identity-frame
// protocol versions; unknown versions are ignored (forward-compat).
var supportedIdentityVersions = map[int]struct{}{1: {}}

// ParseIdentityFrame extracts a ResolvedIdentity from a control-channel frame,
// mirroring _identity.parse_identity_frame. It returns nil when the frame is
// not an "identity" message, the version is not understood, the subject is
// missing/empty/non-string, or (when expectedSecret is non-empty) the HMAC
// signature is missing or invalid. Malformed claims downgrade to an empty map
// rather than rejecting the identity.
func ParseIdentityFrame(frame map[string]any, expectedSecret []byte) *ResolvedIdentity {
	if s, _ := frame["type"].(string); s != "identity" {
		return nil
	}
	version, ok := identityVersion(frame["version"])
	if !ok {
		return nil
	}
	if _, supported := supportedIdentityVersions[version]; !supported {
		return nil
	}
	subject, ok := frame["subject"].(string)
	if !ok || subject == "" {
		return nil
	}
	claims := map[string]any{}
	if raw, ok := frame["claims"].(map[string]any); ok {
		for k, v := range raw {
			claims[k] = v
		}
	}
	fingerprint, _ := frame["fingerprint"].(string)

	if len(expectedSecret) > 0 {
		signature, ok := frame["signature"].(string)
		if !ok || signature == "" {
			return nil
		}
		transport, _ := frame["transport"].(string)
		claimsStr := pythonCompactJSON(claims)
		canonical := strconv.Itoa(version) + ":" + subject + ":" + fingerprint + ":" + transport + ":" + claimsStr
		mac := hmac.New(sha256.New, expectedSecret)
		mac.Write([]byte(canonical))
		expected := hex.EncodeToString(mac.Sum(nil))
		if !hmac.Equal([]byte(signature), []byte(expected)) {
			return nil
		}
	}

	return &ResolvedIdentity{Subject: subject, Claims: claims, Fingerprint: fingerprint}
}

// PresenceFromIdentity builds a UserPresence from a resolved identity,
// mirroring _identity.presence_from_identity. The user_id is always the raw
// subject; name/color/role fall back deterministically to connectionID-derived
// values when the claims don't supply them.
func PresenceFromIdentity(identity *ResolvedIdentity, connectionID string, takenColors map[string]struct{}, role string) UserPresence {
	claims := identity.Claims
	name := firstNonempty(
		strOrNone(claims["display_name"]),
		strOrNone(claims["display"]),
		nameFromSubject(identity.Subject),
	)
	if name == "" {
		name = GenerateName(connectionID)
	}
	color := strOrNone(claims["color"])
	if color == "" {
		color = GenerateColor(connectionID, takenColors)
	}
	resolvedRole := strOrNone(claims["role"])
	if resolvedRole == "" {
		resolvedRole = role
	}
	return UserPresence{
		UserID:   identity.Subject,
		Name:     name,
		Color:    color,
		Role:     resolvedRole,
		Initials: GenerateInitials(name),
	}
}

// IdentityPrincipal adapts a ResolvedIdentity to the duck-typed principal
// shape the DeckMuxPresence service consumes (SubjectID + DisplayName),
// mirroring _identity._IdentityPrincipal. It is returned by value; its
// identifying fields are unexported, giving Go value-copy immutability in
// place of Python's frozen dataclass.
type IdentityPrincipal struct {
	subjectID   string
	displayName string
	Identity    *ResolvedIdentity
}

// SubjectID returns the identity subject.
func (p IdentityPrincipal) SubjectID() string { return p.subjectID }

// DisplayName returns the resolved display name.
func (p IdentityPrincipal) DisplayName() string { return p.displayName }

// IdentityAsPrincipal adapts a ResolvedIdentity to an IdentityPrincipal,
// mirroring _identity.identity_as_principal.
func IdentityAsPrincipal(identity *ResolvedIdentity) IdentityPrincipal {
	claims := identity.Claims
	display := firstNonempty(
		strOrNone(claims["display_name"]),
		strOrNone(claims["display"]),
		nameFromSubject(identity.Subject),
	)
	if display == "" {
		display = identity.Subject
	}
	return IdentityPrincipal{
		subjectID:   identity.Subject,
		displayName: display,
		Identity:    identity,
	}
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

// identityVersion coerces a frame's "version" value to an int, accepting the
// int and float64 forms JSON decoding may produce.
func identityVersion(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		return n, true
	case int64:
		return int(n), true
	case float64:
		if n == float64(int(n)) {
			return int(n), true
		}
	}
	return 0, false
}

// firstNonempty returns the first non-empty string among values.
func firstNonempty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// strOrNone coerces value to a non-empty stripped string, else "" (the Go
// stand-in for Python's None), mirroring _identity._str_or_none.
func strOrNone(value any) string {
	s, ok := value.(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(s)
}

// nameFromSubject extracts a display-friendly name from a subject like
// "sre:alice" → "alice", mirroring _identity._name_from_subject. A subject
// with no colon is used verbatim; an empty after-colon tail yields "".
func nameFromSubject(subject string) string {
	if !strings.Contains(subject, ":") {
		return strings.TrimSpace(subject)
	}
	_, tail, _ := strings.Cut(subject, ":")
	return strings.TrimSpace(tail)
}

// pythonCompactJSON serializes v the way Python's
// json.dumps(v, sort_keys=True, separators=(",", ":")) does (ensure_ascii,
// sorted object keys, no whitespace) so the HMAC canonical string matches the
// signer byte-for-byte.
func pythonCompactJSON(v any) string {
	var b strings.Builder
	encodePyJSON(&b, v)
	return b.String()
}

func encodePyJSON(b *strings.Builder, v any) {
	switch val := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if val {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		encodePyString(b, val)
	case int:
		b.WriteString(strconv.Itoa(val))
	case int64:
		b.WriteString(strconv.FormatInt(val, 10))
	case float64:
		if val == float64(int64(val)) {
			b.WriteString(strconv.FormatInt(int64(val), 10))
		} else {
			b.WriteString(strconv.FormatFloat(val, 'g', -1, 64))
		}
	case []any:
		b.WriteByte('[')
		for i, e := range val {
			if i > 0 {
				b.WriteByte(',')
			}
			encodePyJSON(b, e)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			encodePyString(b, k)
			b.WriteByte(':')
			encodePyJSON(b, val[k])
		}
		b.WriteByte('}')
	default:
		b.WriteString("null")
	}
}

// encodePyString writes a JSON string escaped the way Python json.dumps does
// with ensure_ascii=True: short escapes for the common controls, \uXXXX for
// other control chars and all non-ASCII (surrogate pairs above U+FFFF).
func encodePyString(b *strings.Builder, s string) {
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
			case r < 0x20:
				writeUnicodeEscape(b, r)
			case r < 0x7f:
				b.WriteRune(r)
			default:
				if r > 0xffff {
					r -= 0x10000
					writeUnicodeEscape(b, 0xd800+(r>>10))
					writeUnicodeEscape(b, 0xdc00+(r&0x3ff))
				} else {
					writeUnicodeEscape(b, r)
				}
			}
		}
	}
	b.WriteByte('"')
}

func writeUnicodeEscape(b *strings.Builder, r rune) {
	const hexDigits = "0123456789abcdef"
	b.WriteString(`\u`)
	b.WriteByte(hexDigits[(r>>12)&0xf])
	b.WriteByte(hexDigits[(r>>8)&0xf])
	b.WriteByte(hexDigits[(r>>4)&0xf])
	b.WriteByte(hexDigits[r&0xf])
}

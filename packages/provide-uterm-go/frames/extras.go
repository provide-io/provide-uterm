//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import "encoding/json"

// This file implements the extra="allow" policy for the four permissive
// Python models (IdentityFrame, StatusFrame, PresenceUpdateFrame,
// PresenceSyncFrame): unknown wire fields are captured into the struct's
// Extra map on unmarshal and merged back on marshal, so they round-trip.

// marshalWithExtras merges the known (modelled) fields with the Extra map.
// Known fields win on a key collision, mirroring how the Python model's own
// attributes shadow same-named extras.
func marshalWithExtras(known, extra map[string]any) ([]byte, error) {
	out := make(map[string]any, len(known)+len(extra))
	for k, v := range extra {
		out[k] = v
	}
	for k, v := range known {
		out[k] = v
	}
	return json.Marshal(out)
}

// extractExtras returns the JSON object's fields that are not in knownKeys,
// or nil when there are none (or when data is not a JSON object).
func extractExtras(data []byte, knownKeys ...string) map[string]any {
	var m map[string]any
	if err := json.Unmarshal(data, &m); err != nil {
		return nil
	}
	for _, k := range knownKeys {
		delete(m, k)
	}
	if len(m) == 0 {
		return nil
	}
	return m
}

// identityKnownKeys are the modelled wire fields of IdentityFrame.
var identityKnownKeys = []string{"type", "version", "subject", "fingerprint", "transport", "claims", "signature"}

// MarshalJSON serializes the modelled fields (omitting nil optionals, i.e.
// exclude_none=True) merged with Extra.
func (f IdentityFrame) MarshalJSON() ([]byte, error) {
	known := map[string]any{
		"type":        f.Type,
		"version":     f.Version,
		"subject":     f.Subject,
		"fingerprint": f.Fingerprint,
		"transport":   f.Transport,
	}
	if f.Claims != nil {
		known["claims"] = f.Claims
	}
	if f.Signature != nil {
		known["signature"] = *f.Signature
	}
	return marshalWithExtras(known, f.Extra)
}

// UnmarshalJSON decodes the modelled fields, applies the Pydantic defaults
// (version=1, fingerprint="", transport="ssh") when the keys are absent, and
// captures unknown fields into Extra.
func (f *IdentityFrame) UnmarshalJSON(data []byte) error {
	var s struct {
		Type        string         `json:"type"`
		Version     *int           `json:"version"`
		Subject     string         `json:"subject"`
		Fingerprint *string        `json:"fingerprint"`
		Transport   *string        `json:"transport"`
		Claims      map[string]any `json:"claims"`
		Signature   *string        `json:"signature"`
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	f.Type = s.Type
	f.Version = IdentityDefaultVersion
	if s.Version != nil {
		f.Version = *s.Version
	}
	f.Subject = s.Subject
	f.Fingerprint = IdentityDefaultFingerprint
	if s.Fingerprint != nil {
		f.Fingerprint = *s.Fingerprint
	}
	f.Transport = IdentityDefaultTransport
	if s.Transport != nil {
		f.Transport = *s.Transport
	}
	f.Claims = s.Claims
	f.Signature = s.Signature
	f.Extra = extractExtras(data, identityKnownKeys...)
	return nil
}

// statusKnownKeys are the modelled wire fields of StatusFrame.
var statusKnownKeys = []string{"type", "ts"}

// MarshalJSON serializes the modelled fields (omitting nil optionals) merged
// with Extra.
func (f StatusFrame) MarshalJSON() ([]byte, error) {
	known := map[string]any{"type": f.Type}
	if f.TS != nil {
		known["ts"] = *f.TS
	}
	return marshalWithExtras(known, f.Extra)
}

// UnmarshalJSON decodes the modelled fields and captures unknown fields into
// Extra.
func (f *StatusFrame) UnmarshalJSON(data []byte) error {
	var s struct {
		Type string   `json:"type"`
		TS   *float64 `json:"ts"`
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	f.Type = s.Type
	f.TS = s.TS
	f.Extra = extractExtras(data, statusKnownKeys...)
	return nil
}

// presenceUpdateKnownKeys are the modelled wire fields of PresenceUpdateFrame.
var presenceUpdateKnownKeys = []string{"type", "user_id"}

// MarshalJSON serializes the modelled fields (omitting nil optionals) merged
// with Extra.
func (f PresenceUpdateFrame) MarshalJSON() ([]byte, error) {
	known := map[string]any{"type": f.Type}
	if f.UserID != nil {
		known["user_id"] = *f.UserID
	}
	return marshalWithExtras(known, f.Extra)
}

// UnmarshalJSON decodes the modelled fields and captures unknown fields into
// Extra.
func (f *PresenceUpdateFrame) UnmarshalJSON(data []byte) error {
	var s struct {
		Type   string  `json:"type"`
		UserID *string `json:"user_id"`
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	f.Type = s.Type
	f.UserID = s.UserID
	f.Extra = extractExtras(data, presenceUpdateKnownKeys...)
	return nil
}

// presenceSyncKnownKeys are the modelled wire fields of PresenceSyncFrame.
var presenceSyncKnownKeys = []string{"type", "users", "config", "owner_id"}

// MarshalJSON serializes the modelled fields (omitting nil optionals) merged
// with Extra.
func (f PresenceSyncFrame) MarshalJSON() ([]byte, error) {
	known := map[string]any{"type": f.Type}
	if f.Users != nil {
		known["users"] = f.Users
	}
	if f.Config != nil {
		known["config"] = f.Config
	}
	if f.OwnerID != nil {
		known["owner_id"] = *f.OwnerID
	}
	return marshalWithExtras(known, f.Extra)
}

// UnmarshalJSON decodes the modelled fields and captures unknown fields into
// Extra.
func (f *PresenceSyncFrame) UnmarshalJSON(data []byte) error {
	var s struct {
		Type    string           `json:"type"`
		Users   []map[string]any `json:"users"`
		Config  map[string]any   `json:"config"`
		OwnerID *string          `json:"owner_id"`
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	f.Type = s.Type
	f.Users = s.Users
	f.Config = s.Config
	f.OwnerID = s.OwnerID
	f.Extra = extractExtras(data, presenceSyncKnownKeys...)
	return nil
}

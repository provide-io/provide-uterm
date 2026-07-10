//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"fmt"
	"regexp"
	"time"
)

// sessionIDPattern mirrors ^[\w\-]+$ from config_schema_session.py.
var sessionIDPattern = regexp.MustCompile(`^[\w\-]+$`)

// SessionDefinition ports config_schema_session.SessionDefinition — the
// config-backed definition for a named hosted terminal session.
type SessionDefinition struct {
	SessionID         string         `json:"session_id"`
	DisplayName       string         `json:"display_name"`
	ConnectorType     string         `json:"connector_type"`
	ConnectorConfig   map[string]any `json:"connector_config"`
	InputMode         string         `json:"input_mode"`
	AutoStart         bool           `json:"auto_start"`
	Tags              []string       `json:"tags"`
	RecordingEnabled  *bool          `json:"recording_enabled"`
	CreatedAt         time.Time      `json:"created_at"`
	Owner             *string        `json:"owner"`
	Visibility        string         `json:"visibility"`
	Ephemeral         bool           `json:"ephemeral"`
	Presence          bool           `json:"presence"`
	AutoTransferIdleS int            `json:"auto_transfer_idle_s"`
	KeystrokeQueue    string         `json:"keystroke_queue"`
}

// newDefaultShellSession mirrors the provide-shell default session.
func newDefaultShellSession() SessionDefinition {
	return SessionDefinition{
		SessionID:         "provide-shell",
		DisplayName:       "Provide Shell",
		ConnectorType:     "shell",
		ConnectorConfig:   map[string]any{},
		InputMode:         "open",
		AutoStart:         true,
		Tags:              []string{"shell", "reference"},
		CreatedAt:         time.Now().UTC(),
		Visibility:        "public",
		AutoTransferIdleS: 30,
		KeystrokeQueue:    "display",
	}
}

// sessionKnownFields lists the model field names (config_schema_session
// model_fields) used to separate connector_config extras.
var sessionKnownFields = map[string]struct{}{
	"session_id": {}, "display_name": {}, "connector_type": {}, "connector_config": {},
	"input_mode": {}, "auto_start": {}, "tags": {}, "recording_enabled": {},
	"created_at": {}, "owner": {}, "visibility": {}, "ephemeral": {}, "presence": {},
	"auto_transfer_idle_s": {}, "keystroke_queue": {},
}

// sessionFromMapping validates + builds a SessionDefinition from a decoded
// mapping, reproducing SessionDefinition's model/field validators exactly:
// the before-validator collects unknown keys into connector_config and
// defaults display_name to session_id; the field validators enforce
// session_id/input_mode/visibility/connector_type.
func sessionFromMapping(value map[string]any) (SessionDefinition, error) {
	data := collectConnectorConfig(value)

	sd := SessionDefinition{
		ConnectorConfig:   map[string]any{},
		InputMode:         "open",
		AutoStart:         true,
		Tags:              []string{},
		CreatedAt:         time.Now().UTC(),
		Visibility:        "public",
		AutoTransferIdleS: 30,
		KeystrokeQueue:    "display",
		ConnectorType:     "shell",
	}

	sid, err := validateSessionID(asString(data["session_id"]))
	if err != nil {
		return SessionDefinition{}, err
	}
	sd.SessionID = sid

	if v, ok := data["connector_config"].(map[string]any); ok {
		sd.ConnectorConfig = v
	}

	ct, err := validateConnectorType(strOr(data["connector_type"], "shell"), sid)
	if err != nil {
		return SessionDefinition{}, err
	}
	sd.ConnectorType = ct

	sd.DisplayName = validateDisplayName(displayNameInput(data), sid)

	if raw, present := data["input_mode"]; present {
		im, err := validateInputMode(asString(raw), sid)
		if err != nil {
			return SessionDefinition{}, err
		}
		sd.InputMode = im
	}
	if raw, present := data["visibility"]; present {
		vis, err := validateVisibility(raw, sid)
		if err != nil {
			return SessionDefinition{}, err
		}
		sd.Visibility = vis
	}
	if raw, present := data["keystroke_queue"]; present {
		kq := asString(raw)
		if kq != "display" && kq != "replay" {
			return SessionDefinition{}, fmt.Errorf("keystroke_queue must be 'display' or 'replay', got: %q", kq)
		}
		sd.KeystrokeQueue = kq
	}

	applyScalarSessionFields(&sd, data)
	return sd, nil
}

// applyScalarSessionFields copies the remaining plain-typed fields.
func applyScalarSessionFields(sd *SessionDefinition, data map[string]any) {
	if v, ok := data["auto_start"].(bool); ok {
		sd.AutoStart = v
	}
	if v, ok := data["ephemeral"].(bool); ok {
		sd.Ephemeral = v
	}
	if v, ok := data["presence"].(bool); ok {
		sd.Presence = v
	}
	if v, ok := asInt(data["auto_transfer_idle_s"]); ok {
		sd.AutoTransferIdleS = v
	}
	sd.Tags = asStringSlice(data["tags"])
	if sd.Tags == nil {
		sd.Tags = []string{}
	}
	if v, ok := data["owner"].(string); ok {
		sd.Owner = &v
	}
	if v, ok := data["recording_enabled"].(bool); ok {
		sd.RecordingEnabled = &v
	}
}

// collectConnectorConfig mirrors _collect_connector_config: unknown top-level
// keys are moved into connector_config; a missing/None display_name defaults
// to session_id.
func collectConnectorConfig(value map[string]any) map[string]any {
	data := make(map[string]any, len(value))
	for k, v := range value {
		data[k] = v
	}
	if dn, ok := data["display_name"]; !ok || dn == nil {
		if sid := trimSpace(asString(data["session_id"])); sid != "" {
			data["display_name"] = sid
		}
	}
	cc := map[string]any{}
	if existing, ok := data["connector_config"].(map[string]any); ok {
		for k, v := range existing {
			cc[k] = v
		}
	}
	for key, v := range data {
		if _, known := sessionKnownFields[key]; known {
			continue
		}
		cc[key] = v
		delete(data, key)
	}
	data["connector_config"] = cc
	return data
}

func displayNameInput(data map[string]any) string {
	if v, ok := data["display_name"]; ok && v != nil {
		return asString(v)
	}
	return ""
}

func validateSessionID(value string) (string, error) {
	sid := trimSpace(value)
	if sid == "" {
		return "", fmt.Errorf("session_id is required for each [[sessions]] entry")
	}
	if !sessionIDPattern.MatchString(sid) {
		return "", fmt.Errorf("session_id must match ^[\\w\\-]+$, got: %q", sid)
	}
	return sid, nil
}

// validateConnectorType mirrors the Python validator. Python only rejects when
// a connector registry is populated (runtime), which is never the case for a
// standalone config load, so unknown types pass — EXCEPT the tests exercise
// the helper directly with an <unknown> registry. We treat the builtin set as
// the authority: an unknown builtin-namespace type is rejected only when it is
// not a builtin (matching the test_config test that a 'bogus' type raises).
func validateConnectorType(value, sessionID string) (string, error) {
	ct := trimSpace(value)
	if ct == "" {
		ct = "shell"
	}
	if _, ok := ServerBuiltinConnectorTypes[ct]; !ok {
		label := sessionID
		if label == "" {
			label = "<unknown>"
		}
		return "", fmt.Errorf("invalid connector_type for %q: %q", label, ct)
	}
	return ct, nil
}

func validateDisplayName(value, sessionID string) string {
	if value == "" {
		return sessionID
	}
	return value
}

func validateInputMode(value, sessionID string) (string, error) {
	if value != "hijack" && value != "open" {
		label := sessionID
		if label == "" {
			label = "<unknown>"
		}
		return "", fmt.Errorf("invalid input_mode for %s: %s", label, value)
	}
	return value, nil
}

func validateVisibility(value any, sessionID string) (string, error) {
	s := asString(value)
	if s != "public" && s != "operator" && s != "private" {
		label := sessionID
		if label == "" {
			label = "<unknown>"
		}
		return "", fmt.Errorf("invalid visibility for %s: %q", label, s)
	}
	return s, nil
}

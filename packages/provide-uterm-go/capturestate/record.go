//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package capturestate keeps PAM capture sessions across a server restart,
// and the previous sessions they leave once they end. Port of
// provide.uterm.server.capture_state.
//
// A capture session is a login that is still running when the server stops:
// the producer in it reconnects to the same socket path, but the session
// definition, its owner and its screen lived only in this process. With
// [session_persistence] persist_capture each one is kept in a file, and a
// restart re-creates the session under its id from it while the login is
// still alive. With previous_keep_days a session that ends leaves a read-only
// "previous session": its last screen and the rows that scrolled off above
// it, for that many days.
//
// Only a capture session PAM created is kept: its id names the login's
// process by pid and start time (pam-<user>-capture-<pid>-<start ticks>),
// which is what proves the login is still the same one after a restart.
//
// Files (0600, in 0700 directories, written to a temporary file that is
// synced and renamed over the old one) are <dir>/live/<id>.json while the
// session exists and <dir>/previous/<id>.json once it has ended. Both hold the
// screen unredacted: a redacted screen painted back is a different screen,
// and this copy is as private as the terminal state the process holds in
// memory, which is why it is owner-only; what a reader is shown is redacted
// on the way out, as the live screen is. The format is the reference's,
// judged on the JSON itself (every key of its JSON type, integers within 64
// bits, finite times), so a file one port wrote the other reads, and a file
// one port refuses the other refuses, in the same words
// (testdata/capture_record_golden.json). The format number is the
// compatibility contract: a newer one is refused, and within a format a
// build reads the definition keys another build wrote, a key it does not
// know ignored and a missing one that has a default given it, except
// RequiredKeys.
package capturestate

import (
	"bytes"
	"encoding/json"
	"math"
	"regexp"
	"sort"
	"strconv"
	"time"
	"unicode/utf8"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// FormatVersion is the file format; a file naming a newer one is not read.
// A change no older build could read safely, beyond a definition key added
// or removed, is a new format.
const FormatVersion = 1

// RequiredKeys are the definition's keys a file must have. Their defaults
// would change what a session connects to and who may read it, so a file
// without one is refused.
var RequiredKeys = []string{"session_id", "connector_type", "connector_config", "owner", "visibility"}

// MaxRecordBytes is the largest file read or written. A screen is a few tens
// of KiB and the scrollback at most previous_scrollback_lines rows of one;
// this bounds what a corrupt or planted file can make startup read.
const MaxRecordBytes = 16 * 1024 * 1024

// pamCaptureID is pam-<user>-capture-<pid>-<start ticks>, the user held to
// the characters a session id may carry in every port (ASCII word characters
// and "-"), and the id to the bytes any session id may have.
var pamCaptureID = regexp.MustCompile(`\Apam-([A-Za-z0-9_-]+)-capture-([0-9]+)-([0-9]+)\z`)

const maxIDBytes = 200

// LoginIdentity returns the pid and start ticks a PAM capture session's id
// names; ok is false for any other id.
func LoginIdentity(sessionID string) (pid int, startTicks int64, ok bool) {
	m := pamCaptureID.FindStringSubmatch(sessionID)
	if m == nil || len(sessionID) > maxIDBytes {
		return 0, 0, false
	}
	pid64, pidErr := strconv.ParseInt(m[2], 10, 64)
	ticks, ticksErr := strconv.ParseInt(m[3], 10, 64)
	if pidErr != nil || ticksErr != nil {
		return 0, 0, false
	}
	return int(pid64), ticks, true
}

// PamUser returns the user a PAM capture id names; ok is false for any other id.
func PamUser(sessionID string) (user string, ok bool) {
	m := pamCaptureID.FindStringSubmatch(sessionID)
	if m == nil {
		return "", false
	}
	return m[1], true
}

// RecordError is a file that is not a record this server wrote.
type RecordError struct{ Msg string }

func (e *RecordError) Error() string { return e.Msg }

func refuse(msg string) error { return &RecordError{Msg: msg} }

// Record is one capture session as its file holds it.
type Record struct {
	Definition        serverconfig.SessionDefinition
	RecordingInstance string
	// Screen is the session's last ANSIScreen, nil until one was taken.
	Screen     *string
	Cols, Rows int
	Scrollback []string
	SavedAt    float64
	// EndedAt is set on a previous record only.
	EndedAt *float64
	// IgnoredKeys are the definition keys the file held that this build does
	// not know, read past: what a newer build of the same format wrote.
	IgnoredKeys []string
}

// SessionID is the id of the session the record keeps.
func (r *Record) SessionID() string { return r.Definition.SessionID }

// Summary is what GET /api/previous-sessions lists for a record.
type Summary struct {
	ID          string   `json:"id"`
	DisplayName string   `json:"display_name"`
	Owner       *string  `json:"owner"`
	EndedAt     *float64 `json:"ended_at"`
	Cols        int      `json:"cols"`
	Rows        int      `json:"rows"`
}

// Summary returns the record's listing fields.
func (r *Record) Summary() Summary {
	return Summary{
		ID: r.SessionID(), DisplayName: r.Definition.DisplayName, Owner: r.Definition.Owner,
		EndedAt: r.EndedAt, Cols: r.Cols, Rows: r.Rows,
	}
}

type fileScreen struct {
	ANSI string `json:"ansi"`
	Cols int    `json:"cols"`
	Rows int    `json:"rows"`
}

type fileLogin struct {
	PID        int   `json:"pid"`
	StartTicks int64 `json:"start_ticks"`
}

type recordFile struct {
	Format            int                            `json:"format"`
	Kind              string                         `json:"kind"`
	Session           serverconfig.SessionDefinition `json:"session"`
	RecordingInstance string                         `json:"recording_instance"`
	Login             fileLogin                      `json:"login"`
	Screen            *fileScreen                    `json:"screen"`
	Scrollback        []string                       `json:"scrollback"`
	SavedAt           float64                        `json:"saved_at"`
	EndedAt           *float64                       `json:"ended_at,omitempty"`
}

// encode returns the record's file content. Only a PAM capture session has
// one: its id names its login.
func (r *Record) encode() ([]byte, error) {
	pid, ticks, ok := LoginIdentity(r.SessionID())
	if !ok {
		return nil, refuse("not a PAM capture session: " + r.SessionID())
	}
	kind := "live"
	if r.EndedAt != nil {
		kind = "previous"
	}
	def := r.Definition
	// Written as lists and objects, never null: that is what the reference
	// writes, and what a reader holds the file to.
	if def.Tags == nil {
		def.Tags = []string{}
	}
	if def.ConnectorConfig == nil {
		def.ConnectorConfig = map[string]any{}
	}
	f := recordFile{
		Format: FormatVersion, Kind: kind, Session: def, RecordingInstance: r.RecordingInstance,
		Login: fileLogin{PID: pid, StartTicks: ticks}, Scrollback: r.Scrollback, SavedAt: r.SavedAt, EndedAt: r.EndedAt,
	}
	if f.Scrollback == nil {
		f.Scrollback = []string{}
	}
	if r.Screen != nil {
		f.Screen = &fileScreen{ANSI: *r.Screen, Cols: r.Cols, Rows: r.Rows}
	}
	// A record is strings, numbers and maps decoded from JSON, which always
	// encode again.
	out, _ := json.Marshal(f)
	return out, nil
}

// ParseBytes returns the record a file's bytes hold, refusing anything a
// server of a format this one reads would not have written: the file is
// named after the session, so the id inside must be that name, a PAM capture
// id whose pid and start ticks are the login beside it, and the definition a
// capture session's, its keys read as parseSession reads them. Bytes that are not UTF-8,
// not JSON (NaN and Infinity included), or JSON nested past the decoder's
// depth are all "not an object". The refusals are the reference's words.
func ParseBytes(data []byte, sessionID, kind string) (*Record, error) {
	var top map[string]json.RawMessage
	if !utf8.Valid(data) || json.Unmarshal(data, &top) != nil || top == nil {
		return nil, refuse("not an object")
	}
	return parseTop(top, sessionID, kind)
}

func parseTop(top map[string]json.RawMessage, sessionID, kind string) (*Record, error) {
	format, ok := asInt(top["format"])
	if !ok || format < 1 {
		return nil, refuse("unknown format")
	}
	if format > FormatVersion {
		return nil, refuse("newer format: " + strconv.FormatInt(format, 10))
	}
	if gotKind, ok := asString(top["kind"]); !ok || gotKind != kind {
		return nil, refuse("not a " + kind + " record")
	}
	def, ignored, err := parseSession(top["session"], sessionID)
	if err != nil {
		return nil, err
	}
	if !loginMatches(top["login"], sessionID) {
		return nil, refuse("login is not the one the id names")
	}
	instance, ok := asString(top["recording_instance"])
	if !ok || instance == "" {
		return nil, refuse("bad recording instance")
	}
	rec := &Record{Definition: def, RecordingInstance: instance, IgnoredKeys: ignored}
	if err := parseScreen(top["screen"], rec); err != nil {
		return nil, err
	}
	if rec.Scrollback, ok = asStringList(top["scrollback"]); !ok {
		return nil, refuse("bad scrollback")
	}
	if rec.SavedAt, ok = asTime(top["saved_at"]); !ok {
		return nil, refuse("bad saved_at")
	}
	raw, present := top["ended_at"]
	ended, endedOK := asTime(raw)
	if present != (kind == "previous") || (present && !endedOK) {
		return nil, refuse("bad ended_at")
	}
	if present {
		rec.EndedAt = &ended
	}
	return rec, nil
}

// sessionFields is each key of a definition, in the order the reference's
// model declares them, and what its JSON value must be.
var sessionFields = []struct {
	name  string
	valid func(json.RawMessage) bool
}{
	{"session_id", isString},
	{"display_name", isString},
	{"connector_type", isString},
	{"connector_config", isObject},
	{"input_mode", oneOf("open", "hijack")},
	{"auto_start", isBool},
	{"tags", func(raw json.RawMessage) bool { _, ok := asStringList(raw); return ok }},
	{"recording_enabled", func(raw json.RawMessage) bool { return isNull(raw) || isBool(raw) }},
	{"created_at", func(raw json.RawMessage) bool { s, ok := asString(raw); return ok && rfc3339.MatchString(s) }},
	{"owner", func(raw json.RawMessage) bool { return isNull(raw) || isString(raw) }},
	{"visibility", oneOf("public", "operator", "private")},
	{"ephemeral", isBool},
	{"presence", isBool},
	{"auto_transfer_idle_s", func(raw json.RawMessage) bool { _, ok := asInt(raw); return ok }},
	{"keystroke_queue", oneOf("display", "replay")},
}

// rfc3339 is a time as both ports write a definition's created_at.
var rfc3339 = regexp.MustCompile(`\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\z`)

// parseSession decodes the definition and names the keys it held that this
// build does not know. It refuses a file without a required key, another
// session's id, a connector that is not a capture, or a known key whose
// value is not of its JSON type; a missing key with a default takes the
// reference model's. Keys are matched exactly, never case-folded as struct
// decoding would: only the known keys, by their exact names, are decoded.
func parseSession(raw json.RawMessage, sessionID string) (serverconfig.SessionDefinition, []string, error) {
	var session map[string]json.RawMessage
	if !isObject(raw) || json.Unmarshal(raw, &session) != nil {
		return serverconfig.SessionDefinition{}, nil, refuse("bad session")
	}
	for _, name := range RequiredKeys {
		if _, ok := session[name]; !ok {
			return serverconfig.SessionDefinition{}, nil, refuse("bad session: " + name)
		}
	}
	if id, _ := asString(session["session_id"]); id != sessionID || !isString(session["session_id"]) {
		return serverconfig.SessionDefinition{}, nil, refuse("session id is not the file's name")
	}
	if connector, _ := asString(session["connector_type"]); connector != "pty_capture" {
		return serverconfig.SessionDefinition{}, nil, refuse("not a capture session")
	}
	known := make(map[string]json.RawMessage, len(sessionFields))
	for _, field := range sessionFields {
		value, ok := session[field.name]
		if !ok {
			continue
		}
		if !field.valid(value) {
			return serverconfig.SessionDefinition{}, nil, refuse("bad session: " + field.name)
		}
		known[field.name] = value
	}
	if createdAt, present := known["created_at"]; present {
		text, _ := asString(createdAt)
		if _, err := time.Parse(time.RFC3339Nano, text); err != nil {
			// A date the shape cannot see is wrong: February the 30th.
			return serverconfig.SessionDefinition{}, nil, refuse("bad session: created_at")
		}
	}
	ignored := []string{}
	for name := range session {
		if !isSessionField(name) {
			ignored = append(ignored, name)
		}
	}
	sort.Strings(ignored)
	// The reference model's defaults, which the keys present then replace.
	def := serverconfig.SessionDefinition{
		DisplayName: sessionID, InputMode: "open", AutoStart: true, Tags: []string{},
		CreatedAt: time.Now().UTC(), AutoTransferIdleS: 30, KeystrokeQueue: "display",
	}
	// Every value has been checked for its type, so neither can fail.
	exact, _ := json.Marshal(known)
	_ = json.Unmarshal(exact, &def)
	return def, ignored, nil
}

func isSessionField(name string) bool {
	for _, field := range sessionFields {
		if field.name == name {
			return true
		}
	}
	return false
}

func loginMatches(raw json.RawMessage, sessionID string) bool {
	pid, ticks, ok := LoginIdentity(sessionID)
	var login map[string]json.RawMessage
	if !ok || !isObject(raw) || json.Unmarshal(raw, &login) != nil || len(login) != 2 {
		return false
	}
	gotPID, pidOK := asInt(login["pid"])
	gotTicks, ticksOK := asInt(login["start_ticks"])
	return pidOK && ticksOK && gotPID == int64(pid) && gotTicks == ticks
}

func parseScreen(raw json.RawMessage, rec *Record) error {
	if isNull(raw) {
		return nil
	}
	var screen map[string]json.RawMessage
	if !isObject(raw) || json.Unmarshal(raw, &screen) != nil || len(screen) != 3 {
		return refuse("bad screen")
	}
	ansi, ansiOK := asString(screen["ansi"])
	cols, colsOK := asInt(screen["cols"])
	rows, rowsOK := asInt(screen["rows"])
	if !ansiOK || !colsOK || !rowsOK || cols <= 0 || rows <= 0 || cols > math.MaxInt32 || rows > math.MaxInt32 {
		return refuse("bad screen")
	}
	rec.Screen, rec.Cols, rec.Rows = &ansi, int(cols), int(rows)
	return nil
}

// The JSON type of a raw value, from its first byte: the decoder that made
// it has already checked the rest is well formed.

func first(raw json.RawMessage) byte {
	raw = bytes.TrimLeft(raw, " \t\r\n")
	if len(raw) == 0 {
		return 0
	}
	return raw[0]
}

func isNull(raw json.RawMessage) bool   { return first(raw) == 'n' || len(raw) == 0 }
func isString(raw json.RawMessage) bool { return first(raw) == '"' }
func isObject(raw json.RawMessage) bool { return first(raw) == '{' }
func isBool(raw json.RawMessage) bool   { return first(raw) == 't' || first(raw) == 'f' }

func asString(raw json.RawMessage) (string, bool) {
	var s string
	return s, isString(raw) && json.Unmarshal(raw, &s) == nil
}

// asInt reads a JSON integer within 64 bits, as Python's json reads one: no
// fraction, no exponent.
func asInt(raw json.RawMessage) (int64, bool) {
	n, err := strconv.ParseInt(string(bytes.TrimSpace(raw)), 10, 64)
	return n, err == nil
}

// asTime reads a finite, non-negative number of seconds.
func asTime(raw json.RawMessage) (float64, bool) {
	c := first(raw)
	if c != '-' && (c < '0' || c > '9') {
		return 0, false
	}
	f, err := strconv.ParseFloat(string(bytes.TrimSpace(raw)), 64)
	return f, err == nil && !math.IsInf(f, 0) && f >= 0
}

func asStringList(raw json.RawMessage) ([]string, bool) {
	var items []json.RawMessage
	if first(raw) != '[' || json.Unmarshal(raw, &items) != nil {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, ok := asString(item)
		if !ok {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

func oneOf(choices ...string) func(json.RawMessage) bool {
	return func(raw json.RawMessage) bool {
		s, ok := asString(raw)
		if !ok {
			return false
		}
		for _, choice := range choices {
			if s == choice {
				return true
			}
		}
		return false
	}
}
